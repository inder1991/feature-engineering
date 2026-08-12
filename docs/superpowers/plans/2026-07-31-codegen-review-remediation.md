# Codegen Review Remediation Implementation Plan

> **Cross-plan dependency:** Tasks 1–26 are a hard predecessor of generated-project acceptance in
> the 2026-08-01 semantic/profile plans and of all mapping-crosswalk compilation. Shared migration,
> hashing and wiring gates are recorded in
> `docs/architecture/2026-08-01-verified-interfaces-semantic-profiles.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every confirmed finding of the 2026-07-31 adversarial review of the Kedro/PySpark code-generation subsystem (`docs/architecture/2026-07-31-codegen-adversarial-review.md`).

**Architecture:** All fixes are surgical changes inside `src/featuregen/materialize/` (plus one line in `overlay/upload/graph.py`, one migration, one CI job). No new subsystems. Every governed refusal uses the existing closed vocabularies in `codes.py`; two tasks add one enum member each, which is a vocabulary change owned by `codes.py`. The unwired-orchestrator finding (§1 of the report) is **deliberately not in this plan** — it is new product surface with open design questions, listed at the end for a separate brainstorm+plan.

**Tech Stack:** Python 3.11, pytest, psycopg3, fake_spark test stand-in, uv. Rendered artifacts must work under **both supported kedro lines: 0.19.x (the golden `requirements.lock` pins kedro 0.19.9 / pyspark 3.5.1) and kedro >= 1.0** (user directive 2026-08-01). Version-sensitive emitted code (hooks, `_RUN_SCRIPT`) reads both API shapes; the L0 gate validates under both.

## Global Constraints

- Branch from `origin/main` (verified at `28305084`; all cited line numbers verified there — nothing under `src/featuregen/materialize/`, `tests/featuregen/materialize/`, `src/featuregen/data_agent/`, or `overlay/upload/graph.py`/`read_scope.py` changed since the review commit `a2203bc9`). Work in a worktree via `superpowers:using-git-worktrees`.
- **Read every function fully before editing it** (standing rule from `docs/architecture/2026-07-27-verified-interfaces-materialization.md`). Line numbers below are anchors, not blind edit targets.
- **Migration 1044 is reserved exclusively for Task 15** by the shared interface ledger. Tasks in
  this plan may not consume 1045+; the semantic/profile plans reserve those names. Recheck the full
  applied filename/checksum set at Task 0 because duplicate prefixes already exist.
- Closed failure vocabularies (§14): a governed refusal is always `MaterializationRefused(code, detail)` with a member of the enums in `src/featuregen/materialize/codes.py`. Bare `ValueError`/`TypeError` only for call-site assembly errors, matching the documented doctrine.
- One hasher: `canonical.materialize_hash`. Never introduce a second canonicalization.
- Test commands: `uv run pytest tests/featuregen/materialize -q` for the package; `uv run pytest -q` (full suite) must be green before the branch is offered for merge. Frontend not touched.
- Golden regeneration procedure (until Task 26 adds the guard): delete the stale golden file, run the one test with the new renderer output, eyeball the diff (`git diff`), commit the golden **in the same commit** as the renderer change. After Task 26: run with `UPDATE_GOLDENS=1`.
- **No deploy, no catalog upload** (standing instruction). Code + tests, then stop.
- Commit style: `fix(materialize): <what>` / `feat(materialize): <what>` / `test(materialize): <what>`, matching `git log`.
- Two flagged product-behavior decisions the user should confirm at plan review: **Task 5** (blank uploaded cardinality stops defaulting to `N:1`; such joins will refuse until cardinality is supplied) and **Task 12** (a formula ref naming a column the catalog does not govern now refuses at compile instead of deferring to L1).

---

## Phase A — the artifact runs and is validated

### Task 1: Rendered hook reads the kedro-0.19 run-params key

The rendered `RunParametersHook` reads `run_params["runtime_params"]`, but kedro 0.19.9 (the artifact's pin) passes the dict with key `extra_params` — so every run of a rendered project refuses with `RUN_PARAMETERS_MISSING`. Kedro 1.x uses `runtime_params`. Read both.

**Files:**
- Modify: `src/featuregen/materialize/render/project.py:775`
- Modify: `tests/featuregen/materialize/goldens/cif_daily/src/sandbox_feature_cif_daily/hooks.py` (regenerate)
- Test: `tests/featuregen/materialize/test_render_project.py`

**Interfaces:** none new — the emitted hook text changes.

- [ ] **Step 1: Write the failing test** (in `test_render_project.py`, near the existing hook-text tests):

```python
def test_the_hook_reads_both_kedro_run_param_keys(compiled):
    # kedro 0.19.x passes the dict as "extra_params"; kedro 1.x as "runtime_params".
    # The artifact pins 0.19.9 (goldens/cif_daily/requirements.lock), so reading only
    # the 1.x key means every run refuses RUN_PARAMETERS_MISSING.
    files = _render_files(compiled)          # use this file's existing render helper
    hooks = files["src/sandbox_feature_cif_daily/hooks.py"]
    assert 'get("runtime_params")' in hooks
    assert 'get("extra_params")' in hooks
```

Adapt the helper name to whatever this test file already uses to get rendered file contents (it renders the full project in several tests — reuse, don't invent).

- [ ] **Step 2: Run it, verify it FAILS** (`extra_params` absent): `uv run pytest tests/featuregen/materialize/test_render_project.py -q -k both_kedro`

- [ ] **Step 3: Fix the emission** at `project.py:775`. Replace:

```python
'        supplied = set((run_params or {}).get("runtime_params") or {})\n'
```

with:

```python
'        params = run_params or {}\n'
'        supplied = set(params.get("runtime_params") or params.get("extra_params") or {})\n'
```

- [ ] **Step 4: Regenerate the cif_daily golden** (`hooks.py` will differ): delete `tests/featuregen/materialize/goldens/cif_daily/src/sandbox_feature_cif_daily/hooks.py`, run `uv run pytest tests/featuregen/materialize/test_render_project.py -q`, review the diff, confirm only the two-line hook change.

- [ ] **Step 5: Full package green + commit**: `uv run pytest tests/featuregen/materialize -q` then `git commit -m "fix(materialize): rendered hook reads extra_params — kedro 0.19 cannot run the artifact otherwise"`

### Task 2: Pin ANSI mode in the rendered spark.yml

The overflow gate and the `null_input: zero` coalesce are correct only under `spark.sql.ansi.enabled=false` (the pyspark 3.5 default; 4.x defaults **true**). The rendered `conf/base/spark.yml` pins only app name and timezone, so a cluster-level default silently changes governed numeric semantics into raw `SparkArithmeticException`s outside the gate-code vocabulary.

**Files:**
- Modify: `src/featuregen/materialize/render/project.py:926-942` (`_render_spark`)
- Modify: `tests/featuregen/materialize/goldens/cif_daily/conf/base/spark.yml` (regenerate)
- Test: `tests/featuregen/materialize/test_render_project.py`

- [ ] **Step 1: Failing test:**

```python
def test_spark_yml_pins_ansi_off(compiled):
    files = _render_files(compiled)
    spark_yml = files["conf/base/spark.yml"]
    assert 'spark.sql.ansi.enabled: "false"' in spark_yml
```

- [ ] **Step 2: Run, verify FAIL.**

- [ ] **Step 3: Implement** in `_render_spark` — after the `spark.sql.session.timeZone` line (942) add a comment + setting, matching the function's existing commented style:

```python
        "#\n"
        "# ANSI mode changes what the emitted gates observe: the OVERFLOW_VIOLATION gate\n"
        "# reads a NULL from an out-of-range cast (legacy semantics); under ANSI the cast\n"
        "# raises a raw SparkArithmeticException outside the closed gate vocabulary. The\n"
        "# governed semantics therefore must not depend on a cluster default.\n"
        'spark.sql.ansi.enabled: "false"\n'
```

- [ ] **Step 4: Regenerate the golden spark.yml** (delete + rerun + diff review). Confirm `test_render_project.py:653`-area tests (no-overwrite-key assertions) still pass.

- [ ] **Step 5: Package green + commit**: `git commit -m "fix(materialize): pin spark.sql.ansi.enabled=false in the rendered session config"`

### Task 3: Run the L0 build gate in CI, under the artifact's own pins

`run_l0` (imports the rendered project in a separate interpreter, builds the pipeline) exists and runs **nowhere**: `l0_gate.py` is deliberately not collected, CI runs only `uv run pytest -q`, and the local `.venv-l0` uses kedro 1.5.0/pyspark 4.2.0 — not the kedro 0.19.9/pyspark 3.5.1 the artifact pins. Task 1's bug is exactly what this gate exists to catch and could not.

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `Makefile`
- Read first: `tests/featuregen/materialize/l0_gate.py` (invocation contract: `FEATUREGEN_L0_PYTHON=<venv python> pytest tests/featuregen/materialize/l0_gate.py`)

- [ ] **Step 1: Add a Makefile target** (alongside the existing `test` target):

```makefile
l0-gate:  ## Build-verify the golden Kedro project under the artifact's own pins
	test -x .venv-artifact/bin/python || (uv venv .venv-artifact --python 3.11 && \
		.venv-artifact/bin/python -m pip install --quiet -r tests/featuregen/materialize/goldens/cif_daily/requirements.lock)
	FEATUREGEN_L0_PYTHON=$(PWD)/.venv-artifact/bin/python \
	PYSPARK_PYTHON=$(PWD)/.venv-artifact/bin/python \
	PYSPARK_DRIVER_PYTHON=$(PWD)/.venv-artifact/bin/python \
		uv run pytest tests/featuregen/materialize/l0_gate.py -q
```

- [ ] **Step 2: Run it locally**: `make l0-gate`. Expected after Task 1: PASS. If it fails for a reason other than the Task-1 key (e.g. a kedro-0.19 API used by `_BUILD_PROBE`), that is a real finding — fix forward in this task, keeping `validation.py`'s probe compatible with both kedro majors.

- [ ] **Step 2b: Second gate run under kedro >= 1.0** (user directive 2026-08-01: kedro 1.0+ is a supported target, not just tolerated). Add a second venv + invocation to the `l0-gate` target: `.venv-l0-modern` built from the modern line `l0_gate.py`'s docstring already names (kedro 1.5.0, kedro-datasets 9.5.0, pyspark 4.2.0, python 3.11) via `uv venv .venv-l0-modern --python 3.11 && .venv-l0-modern/bin/python -m pip install --quiet "kedro==1.5.0" "kedro-datasets[spark]==9.5.0" "pyspark==4.2.0"`, then run the same pytest invocation with `FEATUREGEN_L0_PYTHON` pointing at it. Both runs must pass — the golden project builds under both supported kedro lines.

- [ ] **Step 3: Add the CI job** to `.github/workflows/ci.yml` as a sibling of the existing test job (same checkout/uv setup steps):

```yaml
  l0-gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-java@v4
        with: { distribution: temurin, java-version: "17" }
      - uses: astral-sh/setup-uv@v5
        with: { python-version: "3.11" }
      - run: uv sync --extra dev
      - run: make l0-gate
```

- [ ] **Step 4: Commit**: `git commit -m "feat(ci): run the L0 build gate under the rendered artifact's own engine pins"` (CI proof lands when the branch is pushed; note the result in the PR.)

---

## Phase B — silent wrong numbers

### Task 4: Runtime uniqueness gate on every traversal hop

Same-catalog hops emit no runtime fan-out check (only `hop.directional` bridge hops do, at `nodes_compute.py:1668-1681`); a duplicated dimension key silently doubles every SUM and nothing observable moves. Cross-catalog hops already have exactly the right gate shape in `nodes_join_gate.py:131-146` — reuse it inline for every hop.

**Files:**
- Modify: `src/featuregen/materialize/render/nodes_compute.py` (`_traversal_lines`, ~1658-1681)
- Modify: `tests/featuregen/materialize/goldens/traversal_nodes/*.py` (regenerate)
- Test: `tests/featuregen/materialize/test_render_nodes_compute.py`

- [ ] **Step 1: Failing behavioral test** (in the traversal section of `test_render_nodes_compute.py`, using its existing builders — `compiled` fixture, `_run`, and the ACCOUNTS-style dimension fixtures around line 2391):

```python
def test_a_duplicated_dimension_key_raises_JOIN_AMPLIFICATION(compiled):
    # A double-loaded dimension row is the classic silent SUM-doubler. The declared
    # N:1 is metadata; this gate is the first check against the data itself.
    node = _traversal_node(compiled)                 # reuse this section's helper
    rows, accounts, customers = _traversal_frames()  # reuse; then duplicate one key:
    accounts = accounts + [dict(accounts[0])]        # same acct_id twice
    with pytest.raises(RuntimeError) as caught:
        _run_traversal(node, rows, accounts, customers)
    assert str(caught.value).startswith(ValidationGateCode.JOIN_AMPLIFICATION.value)
```

Adapt helper names to the section's real ones (the two-hop tests near `:2489` show the invocation pattern). Also add the inverse assertion to an existing green-path test: unique keys still pass.

- [ ] **Step 2: Run, verify FAIL** (no exception raised — the join silently fans out).

- [ ] **Step 3: Implement** in `_traversal_lines`: after the `hop.frame` select (line 1665) and before the join (1671), emit for **every** hop (directional or not) a pre-join uniqueness gate modeled on `nodes_join_gate.py:136-144`:

```python
        grouped_keys = ", ".join(f"F.col({key!r})" for key in hop.keys)
        lines.extend([
            f"    duplicate_{hop.index} = {hop.frame}.groupBy({grouped_keys}).count().where(",
            "        F.col('count') > F.lit(1)).limit(1).count()",
            f"    if duplicate_{hop.index}:",
            "        raise RuntimeError(",
            f"            {ValidationGateCode.JOIN_AMPLIFICATION.value!r} + ",
            f"            ': join key is not unique on {hop.schema}.{hop.table} for hop {hop.index}'",
            "        )",
        ])
```

Keep the existing directional row-count guard as-is (it additionally catches predicate-scoped amplification). Update the emitted comment at ~1585 ("the traversal ANNOTATES rows and never multiplies them") to say the claim is now **checked**, not assumed.

- [ ] **Step 4: Regenerate traversal goldens** (delete `goldens/traversal_nodes/*.py`, rerun, review diff: each hop gains the 6 gate lines).

- [ ] **Step 5: Package green + commit**: `git commit -m "fix(materialize): gate every traversal hop on observed key uniqueness, not declared cardinality"`

### Task 5: Stop fabricating N:1 for blank uploaded cardinality ⚠️ product decision

`overlay/upload/graph.py:93` fills a blank uploaded cardinality with `"N:1"` (docstring calls it "the safe-fan default") — which two admins then rubber-stamp into a VERIFIED fact that runtime trusts absolutely. "We do not know" must not become "it is safe". **Consequence to confirm with the user: uploads that omit cardinality will propose joins that `plan_join` refuses as cardinality-unknown until a human supplies it.**

**Files:**
- Modify: `src/featuregen/overlay/upload/graph.py:85-93` (`governed_join_proposal`)
- Test: the existing tests for this function (locate via `grep -rn "governed_join_proposal" tests/`)

- [ ] **Step 1: Failing test** (in the file the grep finds, following its fixture style):

```python
def test_a_blank_uploaded_cardinality_stays_unknown():
    # A fabricated N:1 would be admitted, confirmed by two admins, and trusted at
    # runtime. Unknown must stay unknown so plan_join refuses until someone decides.
    ref = _proposal_row(cardinality="")      # reuse the file's row builder
    proposal = governed_join_proposal(ref)
    assert proposal.cardinality is None
```

- [ ] **Step 2: Run, verify FAIL** (currently `"N:1"`).

- [ ] **Step 3: Implement**: change `graph.py:93` from `cardinality=row.cardinality or "N:1"` to `cardinality=row.cardinality or None`. If `ApprovedJoinRef.cardinality` is typed `str`, widen to `str | None`. Update the "safe-fan default" docstring to state the new rule. Chase compile errors: any consumer assuming non-None must treat None as UNKNOWN (that is `join_path._cardinality_verdict`'s existing NULL branch — verify, don't re-implement).

- [ ] **Step 4: Full suite** (`uv run pytest -q` — this touches overlay, not just materialize). Fix any test that pinned the fabricated default by updating it to the new contract.

- [ ] **Step 5: Commit**: `git commit -m "fix(overlay): a blank uploaded join cardinality stays unknown instead of defaulting to N:1"`

### Task 6: ActivePopulation gets the same vintage refusal as CurrentSnapshot

An `ActivePopulation` spine with `availability_ref=None` renders with **no point-in-time predicate at all** — a January backfill run in July silently uses July's actives. `runprep.spine_input_request` (611-689) gives `CurrentSnapshot` a vintage guard ("a table that holds no history cannot answer another date") and `ActivePopulation` — the other present-tense, history-free policy — nothing; `test_runprep.py:929` currently blesses the gap.

**Files:**
- Modify: `src/featuregen/materialize/runprep.py:657-676` (the CurrentSnapshot branch of `spine_input_request`)
- Modify: `tests/featuregen/materialize/test_runprep.py:929` (the blessing test)
- Test: `tests/featuregen/materialize/test_runprep.py`, `tests/featuregen/materialize/test_spine.py`

- [ ] **Step 1: Read `spine_input_request` fully** (611-689). The CurrentSnapshot branch derives the table's observed vintage and refuses `SPINE_DECLARATION_REJECTED_BY_FACTS` when it cannot answer `business_dt`. Note exactly which inputs that branch reads (`spine`, `spine_input`, `business_dt`).

- [ ] **Step 2: Rewrite the blessing test into the failing spec.** Replace `test_an_ACTIVE_POPULATION_spine_needs_no_vintage_and_resolves` (:929) with:

```python
def test_an_ACTIVE_POPULATION_spine_is_refused_for_a_date_its_vintage_cannot_answer():
    # status_cd is current-valued: the table can only answer "who is active NOW".
    # Rendering it for another business date is the population-level as-of leak
    # the review confirmed by execution (report §2.1).
    refused = spine_input_request(_active_population_spine(), _spine_input(),
                                  business_dt=_NOT_THE_VINTAGE)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS

def test_an_ACTIVE_POPULATION_spine_resolves_for_its_own_vintage():
    request = spine_input_request(_active_population_spine(), _spine_input(),
                                  business_dt=_THE_VINTAGE)
    assert isinstance(request, RunInputRequest)
```

Build `_active_population_spine()` from the file's existing spine fixtures (the :929 test shows the shape); derive `_THE_VINTAGE`/`_NOT_THE_VINTAGE` the same way the CurrentSnapshot tests at 657-676's test counterparts do.

- [ ] **Step 3: Run, verify the refusal test FAILS** (currently resolves).

- [ ] **Step 4: Implement** — extend the CurrentSnapshot branch's `isinstance` guard to include `ActivePopulation`, reusing its vintage derivation and refusal verbatim (one guard, two policies; do not duplicate the logic). Extend the refusal message to name the policy class. Keep the compile layer (`spine.py`) unchanged — the verifier's recommendation is a run-prep vintage refusal, since requiring an `availability_ref` alone only half-closes the leak (current-valued `status_cd` still drops historically-closed entities).

- [ ] **Step 5: Also pin the render-side behavior**: add to `test_spine.py` a compile-acceptance test documenting that `ActivePopulation` + `availability_ref=None` **compiles** (unchanged) and rendering emits no cutoff — with a comment pointing at the run-prep gate as the enforcement point. This stops the next reviewer re-flagging it as unknown.

- [ ] **Step 6: Package green + commit**: `git commit -m "fix(materialize): an ActivePopulation spine refuses any business date its vintage cannot answer"`

### Task 7: Honest aggregate-overflow detection

The emitted gate `overflowed = staged.where(F.col(c).isNotNull() & typed.isNull())` (nodes_compute.py:2957) only catches overflow introduced by the final cast. Spark's `CheckOverflowInSum` yields NULL **before** the gate for a sum exceeding its own result type (verified empirically), so genuine `overflow=error` violations publish as NULL. Disambiguation: within a group (≥1 source row by construction), `sum` is NULL either because every operand was NULL (`null_input: ignore` — the policy's own answer) or because it overflowed. A non-null-operand count separates the two.

**Files:**
- Modify: `src/featuregen/materialize/render/nodes_compute.py` (`_aggregate_expression` ~2596-2619; `_overflow_lines` ~2932-2967)
- Modify: `tests/featuregen/materialize/goldens/calculation_nodes/*.py` (regenerate)
- Create: `tests/featuregen/materialize/spark_semantics_gate.py` (uncollected, opt-in, like `l0_gate.py`)
- Test: `tests/featuregen/materialize/test_render_nodes_compute.py`

- [ ] **Step 1: Failing text-level test:**

```python
def test_the_overflow_gate_also_reads_the_operand_count(compiled):
    # sum() overflow yields NULL *before* the cast-based check can see it; a NULL
    # sum over >0 non-null operands is overflow, not policy (report §2.4).
    node = _calculation_node(compiled)               # reuse the section's helper
    assert "__operand_count" in node.source
    assert "operand_count') > F.lit(0)" in node.source.replace('"', "'")
```

- [ ] **Step 2: Run, verify FAIL.**

- [ ] **Step 3: Implement.** (a) In `_aggregate_expression` / the `groupBy(...).agg(...)` assembly (2561), additionally aggregate, for every non-COUNT aggregation, `F.count(F.col({operand!r})).alias(f"__{column}__operand_count")` (for `null_input: zero`, count the **pre-coalesce** operand — the coalesced value is never null and would hide nothing, but the raw count documents the same fact; either is sound, pick pre-coalesce and say so in the emitted comment). (b) In `_overflow_lines`, before the cast check, emit:

```python
    f"    agg_overflowed = staged.where(",
    f"        (F.col({count_col!r}) > F.lit(0)) & F.col({column!r}).isNull())",
    "    if agg_overflowed.limit(1).count() > 0:",
    "        raise RuntimeError(",
    f"            {ValidationGateCode.OVERFLOW_VIOLATION.value!r} + ",
    "            ': the aggregate returned NULL over a group with non-null operands — '",
    "            'overflow inside the aggregation, before the publish cast')",
```

then drop the helper column (`staged = staged.drop({count_col!r})`) after both checks. Rewrite the emitted comment at 2949 that currently reasons only about the cast. Apply to SUM/AVG paths; COUNT paths publish BIGINT and are exempt (state that in the code).

- [ ] **Step 4: Behavioral false-positive test** (runnable under fake_spark): all-NULL operand group with `null_input: ignore` must NOT trip the new gate (count is 0) and must publish NULL. Use `_debit`-style rows with `amount=None`.

- [ ] **Step 5: Real-Spark opt-in proof.** Create `spark_semantics_gate.py` mirroring `l0_gate.py`'s header (not named `test_*`, gated on `FEATUREGEN_L0_PYTHON`): one check that builds a 2-row DataFrame of `Decimal("9.99e35")` typed `DECIMAL(38,2)`, runs the rendered calculation node source via the real engine, and asserts the run raises with `OVERFLOW_VIOLATION` in the message. Wire it into `make l0-gate` as a second pytest target. (fake_spark computes exact sums and cannot represent this; the review's empirical verification is the template.)

- [ ] **Step 6: Regenerate calculation goldens, package green, commit**: `git commit -m "fix(materialize): detect aggregate overflow by operand count, not only the publish cast"`

### Task 8: Refuse `half_even` for ratio features until the engine can honor it

Spark's decimal `Divide` applies `CheckOverflow` with hard-coded HALF_UP at the division result scale before the emitted `F.bround` runs; at published scale 6 the bround is a no-op (verified empirically: 1/2000000 → 0.000001, not the HALF_EVEN 0.000000). A governed declaration the engine silently ignores must refuse instead.

**Files:**
- Modify: `src/featuregen/materialize/physical_types.py` (the public resolver that builds `PhysicalType` — read the module first; `_is_nullable` at :314 shows the file's `RatioBody` handling style)
- Modify: `docs/DEFERRED-WORK.md`
- Test: `tests/featuregen/materialize/test_physical_types.py`

- [ ] **Step 1: Failing test** (using this file's existing `_resolved`/builder helpers — see :575 for the pattern):

```python
def test_half_even_on_a_ratio_is_refused_not_silently_half_up():
    # Spark's decimal Divide rounds HALF_UP at the result scale before any explicit
    # bround; a declared half_even is unenforceable for ratios today (report §2.6).
    with pytest.raises(MaterializationRefused) as caught:
        _resolved(_ratio(rounding=RoundingMode.HALF_EVEN))
    assert caught.value.code is CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED

def test_half_even_on_a_plain_aggregate_is_still_allowed():
    assert _resolved(_sum(rounding=RoundingMode.HALF_EVEN)) is not None
```

If `_resolved` returns refusals rather than raising, assert on the returned `MaterializationRefused` instead — match the file's convention.

- [ ] **Step 2: Run, verify FAIL.**

- [ ] **Step 3: Implement**: in the resolver, where the body is a `RatioBody` and the rounding mode is `HALF_EVEN`, refuse `PHYSICAL_TYPE_UNSUPPORTED` with a detail naming the engine mechanism ("Spark decimal division rounds HALF_UP at the result scale before any explicit rounding call; declare half_up, or wait for engine-side support"). Non-ratio bodies keep both modes (post-aggregate rounding of a narrower-scale value has no engine pre-rounding to fight).

- [ ] **Step 4: DEFERRED-WORK entry** under `## A.` as `### A.<next> 🟡 half_even ratios refused — engine rounds first (2026-07-31)` with Trigger: "a consumer needs banker's rounding on a ratio; requires computing the quotient at guaranteed extra scale or a post-division re-quantization proof".

- [ ] **Step 5: Package green + commit**: `git commit -m "fix(materialize): refuse half_even for ratios — Spark divides with HALF_UP before bround runs"`

### Task 9: Refuse DATE-typed clocks outside UTC at run preparation

The emitted window comparison `F.col(clock) >= F.to_utc_timestamp(boundary.cast('timestamp'), zone)` shifts the whole window by a day when the clock column is Hive `DATE` and the governed zone is west of UTC (verified empirically; east-of-UTC is correct by luck). The IR carries no clock dtype — but run preparation holds the `ClusterInventoryV1`, whose `TableLayout.columns` is ordered `(name, physical type)` pairs. Refuse there, fail-closed, until the IR carries physical time types.

**Files:**
- Modify: `src/featuregen/materialize/runprep.py` (`prepare_run`, ~780-805, after snapshots resolve)
- Modify: `docs/DEFERRED-WORK.md`
- Test: `tests/featuregen/materialize/test_runprep.py`

- [ ] **Step 1: Read how `prepare_run` resolves snapshots from `inventory.tables`** — reuse its exact layout-lookup (same key format); do not invent a second one.

- [ ] **Step 2: Failing tests** (inventory fixtures in `test_runprep.py` already build `TableLayout`s — extend one so the event-time column is typed `date`):

```python
def test_a_DATE_typed_clock_with_a_non_utc_zone_is_refused():
    # date-cast lands at midnight UTC; to_utc_timestamp re-reads the boundary as
    # local wall clock — the window drops its first day west of UTC (report §2.3).
    refused = prepare_run(..., inventory=_inventory_with_date_clock(),
                          business_dt=BUSINESS_DT)       # zone America/New_York in the IR fixture
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED

def test_a_DATE_typed_clock_under_utc_still_prepares():
    ...  # same layout, window_timezone == "UTC" -> RunPreparation returned
```

Thread the real `prepare_run` signature from the file's existing tests (620-663 vary business dates — copy their call shape).

- [ ] **Step 3: Run, verify FAIL.**

- [ ] **Step 4: Implement** in `prepare_run`, after snapshots resolve and before the hash: for every run-input request carrying a `pit_spec`, look up the layout for its physical requirement, build `types = {name.strip().lower(): dtype.strip().lower() for name, dtype in layout.columns}`, extract the column names of `event_time_ref` and `availability_ref` (reuse the module's existing ref-parsing helper — the same one that resolves spine refs), and refuse `MaterializationRefused(CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED, ...)` when the type is exactly `"date"` and `pit_spec.window_timezone != "UTC"`. Name the column, the zone, and the mechanism in the detail.

- [ ] **Step 5: DEFERRED-WORK entry**: `### A.<next> 🔴 DATE clocks refused outside UTC — the IR carries no physical time type (2026-07-31)`, Trigger: "first feature over a DATE-typed event column in a non-UTC catalog; fix = carry the clock dtype in PitSpec and emit date-typed comparisons".

- [ ] **Step 6: Package green + commit**: `git commit -m "fix(materialize): refuse DATE-typed clocks outside UTC at run preparation"`

---

## Phase C — governance

### Task 10: Gate 2 and the spine adopt the migration-1032 read-scope predicate

`ir._hidden` (:506-537) and `spine._resolve_nodes` (:538-584) authorize against the raw `sensitivity` tag only. Migration 1032 made `visible_requires <@ %s` (bound with `allowed_classes(roles)`) THE read-scope predicate and converted 18 files; these two were missed — so a governed-`restricted` column with no file tag (28 of 126 FTR columns, including an Emirates ID) compiles for a caller with no reader role. `read_scope.py:87` even rebound `allowed_sensitivities = allowed_classes`, so these sites already compare a both-vocabulary allow-list against a one-vocabulary column.

**Files:**
- Modify: `src/featuregen/materialize/ir.py:506-537` (and the import at :83)
- Modify: `src/featuregen/materialize/spine.py:538-584`
- Test: `tests/featuregen/materialize/test_ir.py`, `tests/featuregen/materialize/test_spine.py`

- [ ] **Step 1: Failing tests.** In `test_ir.py`, next to `test_the_two_sensitivity_axes_are_NOT_conflated` (:685 — read it first; copy its `graph_node` seeding, which sets `effective_restriction`; migration 1032's generated column derives `visible_requires` from it):

```python
def test_a_governed_restricted_untagged_column_needs_restricted_reader():
    # sensitivity=NULL + effective_restriction='restricted' is the shipped FTR shape
    # (migration 1032 header: 28/126 columns incl. an Emirates ID). Gate 2 must read
    # the governed floor, not only the file tag (report §3.1).
    _seed_column(sensitivity=None, effective_restriction="restricted")
    refused = authorize_compilation(conn, irs, spine, roles=())
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT

def test_restricted_reader_clears_the_governed_floor():
    _seed_column(sensitivity=None, effective_restriction="restricted")
    assert isinstance(authorize_compilation(conn, irs, spine, roles=("restricted_reader",)),
                      AuthorizedCompilation)
```

Add the `confidential`/`confidential_reader` pair too, and the mirror-image pair in `test_spine.py` against `validate_spine_declaration`.

- [ ] **Step 2: Run, verify FAIL** (compilation currently authorized).

- [ ] **Step 3: Implement `_hidden`**: replace the tag query + Python filter with the shipped predicate — import `allowed_classes` and `visibility_predicate` from `featuregen.overlay.upload.read_scope` (drop the `allowed_sensitivities` alias import):

```python
    allowed = allowed_classes(roles)
    hidden: list[_ReadElement] = []
    for catalog_source, indexed in by_source.items():
        rows = conn.execute(
            "SELECT lower(object_ref) FROM graph_node "
            "WHERE catalog_source = %s AND lower(object_ref) = ANY(%s) "
            f"AND NOT ({visibility_predicate()})",
            (catalog_source, list(indexed), allowed)).fetchall()
        hidden.extend(indexed[object_ref] for (object_ref,) in rows)
    return tuple(sorted(hidden, key=lambda element: element.logical_ref))
```

Rewrite the docstring: the rule is now genuinely inherited (one predicate, one parameter, per `read_scope.py`'s module contract); a no-row ref stays authorized here (Task 12 owns existence). **Note:** `visible_requires = '{}'` is contained in any allowed list, so untagged/unfloored rows remain visible — semantics preserved.

- [ ] **Step 4: Implement `_resolve_nodes`**: keep the existence half unchanged; change the SELECT to `lower(object_ref), object_ref, (NOT (visible_requires <@ %s)) AS hidden` with the `allowed` list prepended to the parameter tuple, and build the `hidden` list from that boolean instead of the tag comparison. The table node stays in the check (a floored table node refuses), as today.

- [ ] **Step 5: Run the two new tests green, then the package.** Existing tests that seeded only tags keep passing: a tagged column's floor is also projected into `visible_requires` by 1032's generated column — if any fixture DB predates the generated column in the test harness, fix the fixture, not the predicate.

- [ ] **Step 6: Commit**: `git commit -m "fix(materialize): Gate 2 and the spine read visible_requires — the migration-1032 floor now gates compilation"`

### Task 11: Published access requirements include the governed floor's roles

`classify.py`'s access-requirements loop (:155-176) maps only the raw tag through `SENSITIVITY_ROLES`; `RESTRICTION_ROLES` is never consulted, so a governed-`restricted` untagged column publishes an **empty** requirement tuple in the contract — the requirement is supposed to travel with the data.

**Files:**
- Modify: `src/featuregen/materialize/classify.py:155-176`
- Test: `tests/featuregen/materialize/test_classify.py`

- [ ] **Step 1: Failing test** (this file's fixtures already seed both axes — see `test_sensitivity_class_comes_from_effective_restriction` at :126):

```python
def test_access_requirements_carry_the_floor_not_only_the_tag():
    # An untagged restricted column must publish restricted_reader in the contract;
    # today it publishes () and the artifact travels requirement-free (report §3.1).
    classified = classify_read_set(conn, refs)   # seeded: sensitivity=None, floor='restricted'
    assert "restricted_reader" in classified.access_requirements
```

Add the `confidential` case, and one asserting a tagged+floored column publishes the union.

- [ ] **Step 2: Run, verify FAIL.**

- [ ] **Step 3: Implement**: import `RESTRICTION_ROLES` alongside `SENSITIVITY_ROLES`; after the tag loop, extend the same `requirements` set from axis one (the `restriction` dict is already computed at :143-148):

```python
    for ref in ordered:
        role = RESTRICTION_ROLES.get(restriction[ref])
        if role is not None:
            requirements.add(role)
```

(`restriction[ref]` holds the floor-applied rank; `prohibited` never reaches here — it refused above; `unclassified`/base ranks miss the dict and add nothing.) Update the module docstring's axis-two description.

- [ ] **Step 4: Package green + commit**: `git commit -m "fix(materialize): published access requirements include the governed floor's reader roles"`

### Task 12: A formula ref naming an ungoverned column refuses at compile ⚠️ product decision

`ir.py:517-521` deliberately authorizes refs with no `graph_node` row so a missing column reports as L1's `COLUMN_ABSENT` instead of a misleading role error — but L1 has no production caller, so a hallucinated column renders cleanly and dies on the cluster. With a **precise** refusal code, the original objection (operators chasing the wrong privilege) disappears. **Behavior change to confirm: compile now requires every physical ref to exist in the governed catalog.**

**Files:**
- Modify: `src/featuregen/materialize/codes.py` (add `COLUMN_NOT_GOVERNED` to `CompilationRefusalCode`)
- Modify: `src/featuregen/materialize/ir.py` (`_hidden`'s caller path in `authorize_compilation`)
- Test: `tests/featuregen/materialize/test_codes.py`, `tests/featuregen/materialize/test_ir.py`

- [ ] **Step 1: Failing tests:**

```python
def test_a_ref_the_catalog_does_not_govern_refuses_precisely():
    # No graph_node row used to mean "authorized, L1 will catch it" — but L1 is not
    # on any path. A hallucinated column must die at compile, named as what it is
    # (report §3.3), not as a role problem and not on the cluster.
    refused = authorize_compilation(conn, irs_with_unknown_column, spine, roles=ALL_ROLES)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.COLUMN_NOT_GOVERNED
    assert "public.transactions.txn_amt_typo" in refused.detail
```

Also extend `test_codes.py`'s membership test with the new member (that file pins the closed vocabularies — read it and follow its pattern).

- [ ] **Step 2: Run, verify FAIL** (currently authorizes; the enum member does not exist).

- [ ] **Step 3: Implement**: add `COLUMN_NOT_GOVERNED = "COLUMN_NOT_GOVERNED"` to `CompilationRefusalCode` with a docstring line ("a physical read names a column the governed catalog does not describe — §11's L1 would call it COLUMN_ABSENT, but compile must not emit a read nobody governs"). In `authorize_compilation`, alongside the `_hidden` query (same fetch — return found keys too rather than a second round-trip), compute `missing = set(indexed) - {object_ref for (object_ref,) in fetched}` per source and refuse listing every missing ref. Update the `_hidden` docstring paragraph that argued for pass-through — cite the new code.

- [ ] **Step 4: Fix collateral tests**: any test that exercised the "no row = authorized" doctrine (grep `test_ir.py` for it) flips to expecting `COLUMN_NOT_GOVERNED` — update them with a comment naming this change.

- [ ] **Step 5: Package green + commit**: `git commit -m "feat(materialize): compile refuses refs the governed catalog does not describe (COLUMN_NOT_GOVERNED)"`

### Task 13: `LatestAvailableAsOf.effective_time_ref` must be governed, as its docstring claims

The class docstring (spine.py:172-174) says both columns are checked to be "declared, exist, and are governed" — but only `availability_ref` gets the `is_as_of` governance check (`_refuse_ungoverned_availability`, :675-694). An ETL load-timestamp can silently decide which record version wins.

**Files:**
- Modify: `src/featuregen/materialize/spine.py:675-694`
- Test: `tests/featuregen/materialize/test_spine.py`

- [ ] **Step 1: Failing test** (fixtures at :382 `test_an_UNGOVERNED_availability_ref_has_its_own_code` show the governance seeding — the fixture governs `load_ts` only, which is exactly the gap):

```python
def test_an_ungoverned_effective_time_ref_is_refused():
    # The column that decides which record version wins must carry a governed
    # is_as_of fact; the docstring claims this check and the code omits it (§3.2).
    refused = validate_spine_declaration(conn, _scd_declaration(
        effective_time_ref=UNGOVERNED_COLUMN_REF))
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.AVAILABILITY_TIME_NOT_GOVERNED
    assert "effective_time_ref" in refused.detail
```

- [ ] **Step 2: Run, verify FAIL.** Note: the existing green SCD tests may now also fail if their fixtures never governed the effective column — that is the point; govern `effective_dt` in those fixtures (add the `is_as_of` operational fact the same way they govern `load_ts`).

- [ ] **Step 3: Implement**: in `_refuse_ungoverned_availability`, after the availability check, when `declaration.snapshot_policy` is a `LatestAvailableAsOf`, run the identical `read_operational_value(..., "is_as_of")` check for `located.columns[policy.effective_time_ref]` and refuse `AVAILABILITY_TIME_NOT_GOVERNED` with a detail that names `effective_time_ref` explicitly (same code, distinguishable message — the vocabulary member covers "a time column this policy depends on is not governed").

- [ ] **Step 4: Package green + commit**: `git commit -m "fix(materialize): the SCD effective-time column requires the governed is_as_of fact its docstring promised"`

---

## Phase D — execution-layer hardening

### Task 14: The submitter's timeout is a real bound, diagnostics survive, and `_RUN_SCRIPT` speaks both kedro majors

`subprocess.run(capture_output=True, timeout=...)` re-enters `communicate()` with **no** timeout after killing only the direct child — a JVM grandchild holding the inherited pipes blocks `submit()` forever, and the orphaned process keeps writing into `staging_root`. Separately: `detail=(stderr or stdout or "")[-2000:]` discards stdout whenever Spark's chatty stderr is non-empty, and the documented-mandatory `PYSPARK_PYTHON`/`PYSPARK_DRIVER_PYTHON` are never checked. **Added 2026-08-01 (kedro-1.0 support directive): `_RUN_SCRIPT` calls `KedroSession.create(project_path=root, runtime_params=...)` — that kwarg exists only in kedro >= 1.0; under the 0.19.x line it is `extra_params`, so the submitter cannot start a run in a 0.19 artifact venv. Make the script introspect: `import inspect; kwargs = {"runtime_params" if "runtime_params" in inspect.signature(KedroSession.create).parameters else "extra_params": json.loads(params_json)}` and pass `**kwargs`. Add a test asserting the emitted script text contains the introspection (both key names present), alongside the existing `_RUN_SCRIPT` tests.**

**Files:**
- Modify: `src/featuregen/materialize/submit.py:130-171`
- Test: `tests/featuregen/materialize/test_submit.py`

- [ ] **Step 1: Failing tests:**

```python
def test_a_grandchild_holding_the_pipes_cannot_wedge_the_submitter(tmp_path):
    # CPython's run(timeout=) kills the child then drains pipes with NO timeout;
    # a spark-submit grandchild inheriting them blocks forever (report §4).
    stub = tmp_path / "python"
    stub.write_text("#!/bin/sh\n( sleep 120 ) &\nsleep 120\n")
    stub.chmod(0o755)
    submitter = LocalClusterSubmitter(python_executable=str(stub), timeout_seconds=1.0,
                                      env=_PYSPARK_ENV)
    started = time.monotonic()
    outcome = submitter.submit(tmp_path, run_parameters=dict(PREPARED))
    assert time.monotonic() - started < 40.0          # old code: ~120s pipe-drain hang
    assert outcome.completed is False and outcome.returncode is None

def test_both_streams_survive_into_the_detail(tmp_path):
    stub = tmp_path / "python"
    stub.write_text("#!/bin/sh\necho the-cause\necho noise 1>&2\nexit 3\n")
    stub.chmod(0o755)
    outcome = LocalClusterSubmitter(python_executable=str(stub), env=_PYSPARK_ENV)\
        .submit(tmp_path, run_parameters=dict(PREPARED))
    assert "the-cause" in outcome.detail and "noise" in outcome.detail

def test_missing_pyspark_python_is_refused_before_a_process_exists(tmp_path):
    with pytest.raises(ValueError, match="PYSPARK_PYTHON"):
        LocalClusterSubmitter(python_executable="/bin/true", env={})\
            .submit(tmp_path, run_parameters=dict(PREPARED))
```

`PREPARED` is this file's existing fixture; define `_PYSPARK_ENV = {"PYSPARK_PYTHON": "/bin/true", "PYSPARK_DRIVER_PYTHON": "/bin/true"}`.

- [ ] **Step 2: Run, verify FAIL** (first test hangs ~2 min — run with `timeout 200` guard; second loses stdout; third starts the process).

- [ ] **Step 3: Implement `submit()`** (keep `check_run_parameters` first; add `import signal`, `import time`):

```python
        merged = os.environ | self.env if self.env is not None else dict(os.environ)
        missing_env = [name for name in ("PYSPARK_PYTHON", "PYSPARK_DRIVER_PYTHON")
                       if not merged.get(name)]
        if missing_env:
            raise ValueError(
                f"env is missing {missing_env}: without them Spark launches workers on "
                f"whatever python is on PATH and dies deep inside an executor")
        try:
            process = subprocess.Popen(          # noqa: S603 - fixed argv
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                cwd=str(root), env=merged, start_new_session=True)
        except (OSError, subprocess.SubprocessError) as error:
            return SubmissionOutcome(completed=False, returncode=None,
                detail=f"execution never started ({type(error).__name__}): {error}")
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)   # pgid == pid: start_new_session
            try:
                stdout, stderr = process.communicate(timeout=30.0)
            except subprocess.TimeoutExpired:        # a pipe survived even SIGKILL
                stdout, stderr = "", ""
            return SubmissionOutcome(completed=False, returncode=None,
                detail=f"the run exceeded {self.timeout_seconds}s; its process group was "
                       f"killed. stderr: {stderr.strip()[-1000:]}")
        return SubmissionOutcome(
            completed=process.returncode == 0, returncode=process.returncode,
            detail=f"stderr: {stderr.strip()[-1500:]} | stdout: {stdout.strip()[-500:]}")
```

Existing tests asserting on `detail` content will need their expectations updated to the labeled form — update them, don't weaken them.

- [ ] **Step 4: Package green + commit**: `git commit -m "fix(materialize): the submitter kills the process group, bounds the drain, keeps both streams, checks the PYSPARK env"`

### Task 15: The database refuses run events after a terminal one (migration 1044)

`materialization_run_event` accepts a non-terminal event appended after a terminal one (only the four terminal kinds have the partial-unique index), and `seq` is caller-supplied — one out-of-order INSERT bricks `run_status()` forever on an append-only table with no repair path.

**Files:**
- Create: `src/featuregen/db/migrations/1044_run_event_ordering.sql`
- Create: `tests/featuregen/materialize/test_migration_1044.py`

- [ ] **Step 1: Write the migration** (idempotent DDL, like 1034):

```sql
-- 1044: a run's event stream is append-ONLY AND ORDERED. fold_run_status raises
-- forever if an event follows a terminal one, and the append-only triggers from
-- 1034 make that state unrepairable — so the database must refuse the write, not
-- merely the read. Races between concurrent INSERTs for one run are closed by the
-- (run_id, seq) PK plus this trigger's max-seq check running BEFORE INSERT.
CREATE OR REPLACE FUNCTION materialization_run_event_ordered()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM materialization_run_event
        WHERE run_id = NEW.run_id
          AND event_kind IN ('GATES_FAILED', 'PUBLISHED', 'PUBLICATION_REFUSED', 'RUN_FAILED')
    ) THEN
        RAISE EXCEPTION 'materialization_run_event: run % already recorded a terminal event',
            NEW.run_id;
    END IF;
    IF EXISTS (
        SELECT 1 FROM materialization_run_event
        WHERE run_id = NEW.run_id AND seq >= NEW.seq
    ) THEN
        RAISE EXCEPTION 'materialization_run_event: seq % does not extend run %',
            NEW.seq, NEW.run_id;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS materialization_run_event_ordered ON materialization_run_event;
CREATE TRIGGER materialization_run_event_ordered
    BEFORE INSERT ON materialization_run_event
    FOR EACH ROW EXECUTE FUNCTION materialization_run_event_ordered();
```

- [ ] **Step 2: Write the failing tests** in `test_migration_1044.py`, copying `test_migration_1034.py`'s scaffolding exactly (its `_migration_sql()` reader, psycopg fixtures, and the dependency-ordered row seeding starting at :57 — it already seeds a valid generation + run event):

```python
def test_an_event_after_a_terminal_one_is_refused_by_the_database(conn):
    _append(conn, seq=1, kind="RUN_PREPARED")
    _append(conn, seq=2, kind="PUBLISHED")
    with pytest.raises(psycopg.errors.RaiseException, match="terminal"):
        _append(conn, seq=3, kind="COMPUTATION_COMPLETED")

def test_a_seq_that_does_not_extend_the_run_is_refused(conn):
    _append(conn, seq=5, kind="RUN_PREPARED")
    with pytest.raises(psycopg.errors.RaiseException, match="does not extend"):
        _append(conn, seq=4, kind="RUN_SUBMITTED")

def test_the_ordinary_ascending_stream_still_inserts(conn):
    for seq, kind in enumerate(("RUN_PREPARED", "RUN_SUBMITTED", "COMPUTATION_COMPLETED",
                                "GATES_PASSED", "PUBLISHED")):
        _append(conn, seq=seq, kind=kind)
```

`_append` wraps the same INSERT `append_run_event` uses (copy the column list from control_plane.py:387-399); run 1034's SQL then 1044's in the fixture.

- [ ] **Step 3: Run** — the refusal tests FAIL before the migration is applied in the fixture, PASS after. Also extend `append_run_event`'s docstring (control_plane.py:387) to name the new trigger as the third database guarantee.

- [ ] **Step 4: Full suite green** (control-plane tests must still pass — they append in order) **+ commit**: `git commit -m "feat(db): migration 1044 — the run-event stream is ordered, terminal means terminal"`

### Task 16: The newest attestation on a version tuple wins

`select_publisher` (publish.py:540-651) refuses only when **no** matching attestation passed — an old passing probe outlives a newer probe that demonstrated the mechanism now fails on identical versions, contradicting the documented `PUBLISH_MECHANISM_UNSUPPORTED` split. `read_attestations` already orders by `recorded_at`.

**Files:**
- Modify: `src/featuregen/materialize/publish.py` (the `passing = ...` block, ~615-625)
- Test: `tests/featuregen/materialize/test_publish.py`

- [ ] **Step 1: Failing test** (mirror `test_a_later_PASSING_probe_overrides_an_earlier_failure` at :371 — same fixtures, inverted order):

```python
def test_a_later_FAILING_probe_defeats_an_earlier_pass():
    # The most recent evidence on identical engine versions says the mechanism is
    # broken; publication must not proceed on stale success (report §4).
    _record(passed=True)
    _record(passed=False)
    refused = select_publisher(conn, ...)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is PublicationRefusalCode.PUBLISH_MECHANISM_UNSUPPORTED
```

- [ ] **Step 2: Run, verify FAIL** (currently returns a `PublisherSelection` naming the old attestation).

- [ ] **Step 3: Implement**: immediately after `matching` is computed and non-empty, insert:

```python
    if not matching[-1].passed:
        return MaterializationRefused(
            PublicationRefusalCode.PUBLISH_MECHANISM_UNSUPPORTED,
            f"the most recent probe on these engine versions "
            f"({matching[-1].attestation_id}) demonstrated the mechanism failing; "
            f"earlier passing evidence is stale, re-run the probe")
```

Leave the rest (passing / covering selection) unchanged — the existing later-passing-overrides test stays green, and schema-evolution coverage keeps drawing on any passing probe when the newest evidence is good.

- [ ] **Step 4: Package green + commit**: `git commit -m "fix(materialize): a newer failing capability probe defeats stale passing attestations"`

### Task 17: Validation reports are readable, and L0's channels are hardened

`pipeline_validation_report` has an INSERT and no reader anywhere in src, so `may_regenerate`'s blocking rule holds only in-process. And L0: the `@@L0-VERDICT@@ ` marker is a fixed string a generated project could print itself (the docstring at validation.py:346-348 names the attack; zero tests), `timeout_seconds` is untested, and the non-UTF-8 branch of `_files_on_disk` has no test pinning its verdict.

**Files:**
- Modify: `src/featuregen/materialize/validation.py`
- Test: `tests/featuregen/materialize/test_validation.py`

- [ ] **Step 1: Failing round-trip test** (build the report with the same helpers `test_a_report_is_APPENDED_with_its_findings_intact` (:843) uses):

```python
def test_a_recorded_report_reads_back_equal_and_gates_regeneration(conn):
    report = _report_with(_blocking_finding())      # reuse :843's builders
    record_validation_report(conn, report)
    (read,) = read_validation_reports(conn, generation_id=report.generation_id)
    assert read == report
    assert may_regenerate_for(conn, generation_id=report.generation_id) is False
```

- [ ] **Step 2: Implement the reader** — `read_validation_reports(conn, *, generation_id) -> tuple[ValidationReportV1, ...]`: SELECT the eleven columns `record_validation_report` inserts (:318-331), ordered by `started_at`, and rebuild each report; the findings come back through the **exact inverse of `findings_payload()`** (read that method first and mirror its keys — do not guess field names). Then `may_regenerate_for(conn, *, generation_id) -> bool`: read all reports, group by `level`, apply the existing `may_regenerate` to the newest of each level, AND the results; no reports means `True` (nothing recorded blocks). Docstring: this is the cross-process form of the in-memory rule.

- [ ] **Step 3: Nonce the L0 verdict marker.** Failing test first:

```python
def test_a_project_printing_the_verdict_marker_cannot_bless_itself(tmp_path):
    # validation.py:346's own docstring names this attack; nothing tests it.
    _write_probe_stub(tmp_path, prints='@@L0-VERDICT@@ {"builds": true}')
    report = run_l0(tmp_path, python_executable=str(_stub_python(tmp_path)), ...)
    assert report.status is not ValidationStatus.PASSED
```

Implement in `_probe_verdict` (:391-416): derive `marker = f"@@L0-VERDICT@@{uuid.uuid4().hex} "` per invocation and pass it as the probe's argv marker (the probe already receives the marker as `sys.argv[1]` — no probe change needed); scan only for the nonce'd marker. A fixed-string forgery no longer matches.

- [ ] **Step 4: Pin the timeout and the non-UTF-8 verdicts.** Two tests: a probe stub that sleeps past a 1-second `timeout_seconds` must yield a non-PASSED report (not a hang, not an exception escaping); a sealed project with one non-UTF-8 file (`(root/"src"/pkg/"junk.py").write_bytes(b"\xff\xfe")`) must yield a `PROJECT_HASH_MISMATCH` finding whose `observed` names the UTF-8 problem — read the `_files_on_disk`-returns-None caller first; if the current behavior is already fail-closed, the test simply pins it; if it fails open, route `None` into that finding.

- [ ] **Step 5: Package green + commit**: `git commit -m "feat(materialize): validation reports read back and gate regeneration; L0 verdict is nonce'd and its timeout pinned"`

### Task 18: A failed probe's evidence survives the aborted transaction

`DirectSqlExecutor.observe_relationship` (executor.py:87-110) catches the probe exception and builds a failure observation — but on a psycopg connection the transaction is now aborted, so the store's very next INSERT raises `InFailedSqlTransaction` and the evidence is lost. No SAVEPOINT exists in `data_agent/`. The conn is deliberately DB-API-2.0-generic (PyHive/impyla), so the savepoint must be opportunistic.

**Files:**
- Modify: `src/featuregen/data_agent/executor.py:87-110`
- Test: `tests/featuregen/data_agent/` (alongside the existing executor tests — reuse their psycopg fixture)

- [ ] **Step 1: Failing test** (needs a real psycopg conn — use the same DB fixture `test_relationship_observation_store.py` uses):

```python
def test_a_failed_probe_leaves_the_transaction_usable_for_the_failure_record(conn):
    # The whole point of the failure observation is to be PERSISTED; an aborted
    # tx makes it unstorable and buries the cause (report §4).
    executor = DirectSqlExecutor(conn, PostgresDialect())
    observation = executor.observe_relationship(_plan_probing_a_missing_table())
    assert observation.complete is False
    conn.execute("SELECT 1")          # old behavior: InFailedSqlTransaction
```

- [ ] **Step 2: Run, verify FAIL** (`InFailedSqlTransaction` on the SELECT).

- [ ] **Step 3: Implement** — wrap the probe in a savepoint when the borrowed conn offers one, keeping the DB-API-only contract for engines that don't:

```python
        transaction = getattr(self._conn, "transaction", None)
        try:
            if callable(transaction):
                with self._conn.transaction():    # psycopg3: SAVEPOINT when nested
                    row = self._run(plan, statement)
            else:
                row = self._run(plan, statement)
        except Exception as exc:  # noqa: BLE001 — data-side failures are typed coverage
            ...existing failure-observation body unchanged...
```

Document in the class docstring: "on psycopg the probe runs inside a savepoint so a failure never aborts the caller's transaction; on DB-API-only engines (Hive) there is no enclosing Postgres transaction to protect."

- [ ] **Step 4: data_agent tests green + commit**: `git commit -m "fix(data-agent): probe failures roll back to a savepoint so the failure observation can be stored"`

---

## Phase E — identity correctness and small refusals

### Task 19: One governed formula, one `ir_hash`, regardless of ref casing

`compile_expression` resolves the source table with the **raw** ref while `_plan_to_grain` normalizes (expression_ir.py:805 vs :1024) — a mixed-case formula (reachable: `graph_node.object_ref` stores original case and authoring tools hand refs to the model) forks `ir_hash` from the case-folded `formula_content_hash` and duplicates the table's `PhysicalInputRequirement`.

**Files:**
- Modify: `src/featuregen/materialize/expression_ir.py` (the compile entry — normalize once, at the boundary)
- Test: `tests/featuregen/materialize/test_expression_ir.py`

- [ ] **Step 1: Failing test** (use this file's existing formula/IR builders; every fixture is lower-case today — that absence is the finding):

```python
def test_ref_casing_does_not_fork_identity():
    # formula_content_hash folds case, so RETAIL::PUBLIC.TXN.AMT and its lower-case
    # spelling are ONE governed artifact — the IR must agree (report §5).
    lower = _compile(_formula(operand="retail::public.txn.amt"))
    upper = _compile(_formula(operand="RETAIL::PUBLIC.TXN.AMT"))
    assert lower.ir_hash == upper.ir_hash
    assert len(upper.input_requirements) == len(lower.input_requirements)
```

- [ ] **Step 2: Run, verify FAIL** (hashes differ / requirement duplicated).

- [ ] **Step 3: Implement**: at the top of the compile entry point, before any `tables.resolve(...)`, canonicalize every logical ref on the expression (source relation, operand, event/availability refs, grain keys, filter left sides) through the same normalization `_plan_to_grain`'s `_table_ref_of` applies — one helper, applied once, so raw spellings cannot reach either the `_Tables` cache or `identity_payload()`. Do NOT normalize inside `_Tables.resolve` (that would leave payload refs unfolded and only mask the fork).

- [ ] **Step 4: Package green + commit**: `git commit -m "fix(materialize): normalize logical refs at IR compile so casing cannot fork ir_hash"`

### Task 20: SUM over an all-NULL window is nullable

`_is_nullable` (physical_types.py:314-323) misses the fourth NULL source: any non-COUNT aggregate with `null_input=IGNORE` over a non-empty, all-NULL window returns NULL in Spark — the column is typed NOT NULL and the rendered `WRONG_NULLABILITY` gate then aborts a correctly-authored feature the first time the data contains such a group. `test_physical_types.py:575` currently pins the wrong answer.

**Files:**
- Modify: `src/featuregen/materialize/physical_types.py:314-323`
- Modify: `tests/featuregen/materialize/test_physical_types.py:575`
- Test: `tests/featuregen/materialize/test_render_nodes_compute.py` (behavioral)

- [ ] **Step 1: Flip the pinned test and add the spec:**

```python
def test_IGNORE_over_an_all_null_window_is_nullable():
    # F.sum over a group whose every operand is NULL returns NULL — the renderer
    # deliberately does not coalesce it (the marker comment at nodes_compute
    # ~2721 explains why), so the TYPE must admit it (report §5).
    assert _resolved(_sum(null_input=NullInput.IGNORE)).nullable is True
```

(Delete/replace the `is False` assertion at :575, citing this task in the comment.)

- [ ] **Step 2: Run, verify FAIL.**

- [ ] **Step 3: Implement** — add to `_is_nullable`'s loop:

```python
        if (expr.window.null_input is NullInput.IGNORE
                and not _is_count(expr.aggregation)):
            return True
```

with `_is_count` covering the COUNT family (`COUNT_ROWS` and any COUNT_* members of `AggregateFunction` — read the enum; counts return 0, never NULL). Extend the module docstring's NULL-source list from three to four.

- [ ] **Step 4: Behavioral proof** in `test_render_nodes_compute.py`: an all-NULL `fee_amt` group under IGNORE publishes NULL and the `WRONG_NULLABILITY` gate does not fire (build with `_customer`/`_txn(fee_amt=None)` rows through `_run`).

- [ ] **Step 5: Package green + commit**: `git commit -m "fix(materialize): an ignored-nulls aggregate over an all-null window is nullable — Spark says so"`

### Task 21: Identity hygiene batch

Three small identity defects: `materialize_hash` rejects nested non-dict Mappings although `filter_tree` is annotated `Mapping` (a `MappingProxyType` raises from inside a hash); `prepare_run` hashes the caller's **raw** `business_dt` string while canonicalizing it everywhere else (whitespace forks `sandbox_execution_hash`); and `input_snapshots` is a run parameter no rendered node consumes, so the hash's "exact reads performed" claim is false — corrected honestly in docs + deferral, not by a semantics change.

**Files:**
- Modify: `src/featuregen/materialize/canonical.py:28-51`, `src/featuregen/materialize/runprep.py:788-805`
- Modify: `docs/DEFERRED-WORK.md`
- Test: `tests/featuregen/materialize/test_canonical.py`, `tests/featuregen/materialize/test_runprep.py`

- [ ] **Step 1: Failing tests:**

```python
def test_nested_mapping_views_hash_like_plain_dicts():
    from types import MappingProxyType
    plain = {"filter": {"op": "and", "children": [{"left": "x"}]}}
    proxied = {"filter": MappingProxyType({"op": "and",
               "children": [MappingProxyType({"left": "x"})]})}
    assert materialize_hash(plain) == materialize_hash(proxied)
```

```python
def test_business_dt_whitespace_does_not_fork_execution_identity():
    a = prepare_run(..., business_dt="2026-07-27")
    b = prepare_run(..., business_dt=" 2026-07-27 ")
    assert a.parameters["sandbox_execution_hash"] == b.parameters["sandbox_execution_hash"]
```

(copy `prepare_run`'s call shape from the tests at :620-663).

- [ ] **Step 2: Run, verify both FAIL** (first raises `CanonicalizationError`; second's hashes differ).

- [ ] **Step 3: Implement.** In `canonical.py`, deep-convert before dumping:

```python
def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value
```

and `return hashlib.sha256(_jcs_dumps(_plain(dict(payload)))).hexdigest()` (update the inline comment — it currently explains only the top level). In `runprep.py:800-803`, pass `business_dt=covered["business_dt"]` (the canonicalized form) into `sandbox_execution_hash` instead of the raw argument.

- [ ] **Step 4: The `input_snapshots` honesty correction.** In `runprep.parameter_payload`'s docstring (~243-261): replace "prove afterwards that it read precisely them" with the true statement — the snapshot set is resolved at preparation and consumed by L1's `PARTITION_ABSENT` check; the rendered nodes filter by the window/availability predicates and do not re-apply the snapshot list. Add DEFERRED-WORK entry `### A.<next> 🟡 input_snapshots is prepared evidence, not an enforced read scope (2026-07-31)`, Trigger: "§3.4 identity is relied on for audit of what a run read; fix = render partition predicates onto raw sources, or record actually-read partitions post-run".

- [ ] **Step 5: Package green + commit**: `git commit -m "fix(materialize): identity hygiene — nested mappings hash, business_dt canonical in the hash, input_snapshots claim corrected"`

### Task 22: Refusal hygiene batch

Three escapes from the closed-vocabulary doctrine: `derive_group_contract` silently collapses two IRs sharing a `feature_name` (bypassing `MULTIPLE_MATERIALIZATION_CONTRACTS`); a `PARTITION_MAPPED` spine over a non-date-resolvable mapping dies in the renderer as a bare `ValueError` instead of refusing at preparation; and `plan_cross_catalog_join` never reads `cardinality_basis`, so a store-rehydrated revision claiming `MANY_TO_ONE` on `metadata_inference` is admitted as fan-in-safe.

**Files:**
- Modify: `src/featuregen/materialize/contract.py:761-768`, `src/featuregen/materialize/runprep.py` (`spine_input_request`), `src/featuregen/materialize/joins.py:347-367`
- Test: `tests/featuregen/materialize/test_contract.py`, `test_runprep.py`, `test_bridge_joins.py`

- [ ] **Step 1: Three failing tests:**

```python
def test_two_irs_sharing_a_feature_name_cannot_silently_collapse():
    with pytest.raises(ValueError, match="share feature_name"):
        derive_group_contract(conn, _authorization_with_duplicate_names(), ...)
```

```python
def test_a_partition_mapped_spine_over_an_unresolvable_mapping_refuses_at_prep():
    # Today this dies in the renderer as a bare ValueError — outside §14's vocabulary.
    refused = spine_input_request(_partition_mapped_spine(),
                                  _spine_input(partition_mapping=_full_scan()),
                                  business_dt=BUSINESS_DT)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.PARTITION_MAPPING_NOT_DECLARED
```

```python
def test_an_unattested_cardinality_basis_is_refused():
    # "we do not know" is not "it is safe" — the basis records how well the
    # direction is known, and the store rehydrates it independently (report §5).
    revision = _revision(cardinality=MANY_TO_ONE, cardinality_basis=CardinalityBasis.METADATA_INFERENCE)
    refused = plan_cross_catalog_join(_realization(revision), from_identity=..., to_identity=...)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN
```

(Read `CardinalityBasis`'s members in `overlay/upload/bridge_realization.py` first; the attested pair used by the shipped producer is the governed-key/deterministic pair — allow exactly those.)

- [ ] **Step 2: Run, verify all three FAIL.**

- [ ] **Step 3: Implement.** (a) `contract.py`: in the loop at :761, before assignment: `if ir.feature_name in contracts: raise ValueError(f"two IRs in one authorization share feature_name {ir.feature_name!r}: admission refuses this within a batch, so this call was assembled from artifacts that never co-admitted")` — assembly error per the §14 doctrine, matching `authorize_compilation`'s style. (b) `runprep.spine_input_request`: add a `PartitionMappedSnapshot` branch refusing `PARTITION_MAPPING_NOT_DECLARED` when `spine_input.partition_mapping` is not the mapping kind the renderer's `_partition_mapped` can resolve a business date against (read `_partition_mapped` in nodes_compute.py:622-632 and mirror its supported set exactly — one source of truth for the message). The renderer's raise stays as defense-in-depth. (c) `joins.py`: after the `has_unresolved_requirements` check (:362-367), refuse `JOIN_CARDINALITY_UNKNOWN` when `revision.cardinality_basis` is not in the attested-basis allowlist, naming the basis in the detail.

- [ ] **Step 4: Package green + commit**: `git commit -m "fix(materialize): three refusal-hygiene holes — duplicate feature names, unresolvable partition mappings, unattested cardinality bases"`

### Task 23: Render hygiene batch

Four small render defects: `yaml_scalar` escapes only `\` and `"` (a `\n` in a table name folds to a *different table name* silently, and catalog `# {comment}` lines have no escaping at all); dotted schemas mis-split with `split(".", 1)`; the `feature_staging_manifest_` prefix collides with a feature legitimately named `manifest_<x>` (and `assembled` with the fixed dataset); L1's `_read_set`/`denied` keys are never case-folded while every column comparison is.

**Files:**
- Modify: `src/featuregen/materialize/render/_yaml.py`, `src/featuregen/materialize/render/project.py` (:339, :360, :425, :499, :532), `src/featuregen/materialize/validation.py` (L1 region ~737-800)
- Modify: goldens (dataset renames)
- Test: new `tests/featuregen/materialize/test_yaml_scalar.py`, plus `test_render_project.py`, `test_validation.py`

- [ ] **Step 1: Failing tests:**

```python
# test_yaml_scalar.py
import yaml  # dev dependency; if absent, assert on the escaped text instead
def test_control_characters_cannot_change_the_value():
    for hostile in ("cust\nomers", "a\tb", "c\rd", "e\x1bf"):
        scalar = yaml_scalar(hostile)
        assert "\n" not in scalar and "\r" not in scalar and "\t" not in scalar
        assert yaml.safe_load(scalar) == hostile
```

```python
def test_a_dotted_schema_addresses_the_right_table(compiled):
    # split(".", 1) on "edp.raw.customers" yields table "raw.customers" — a table
    # that does not exist. The last dot separates schema from table (report §5).
    ...  # render with a requirement whose schema is "edp.raw"; assert the catalog
         # entry has database: "edp.raw" and table: "customers"

def test_a_feature_named_manifest_x_renders(compiled_with_feature("manifest_x")):
    ...  # today _unique raises a "catalog collision"; after the fix it renders

def test_l1_folds_table_casing_once(...):
    ...  # two IR refs spelling one table RISK.TXN / risk.txn -> ONE can_read call,
         # ONE READ_DENIED finding when denied (count the findings)
```

- [ ] **Step 2: Run, verify FAIL.**

- [ ] **Step 3: Implement.** (a) `_yaml.py` — full escaping:

```python
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t", "\r": "\\r"}

def yaml_scalar(value: str) -> str:
    out: list[str] = []
    for ch in str(value):
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ch == "\x7f":
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'
```

(b) `project.py` `_hive_entry`: route the comment through a one-line guard collapsing `\r`/`\n` to a space before `f"  # {comment}"`. (c) Both `split(".", 1)` sites become `rsplit(".", 1)` with a comment ("the LAST dot separates schema from table; a dotted schema is real, a dotted table is not"). (d) Dataset prefixes: `manifests[column] = f"feature_manifest__{column}"` (:339) and `assembled="feature_group__assembled"` (:360) — the double-underscore families cannot collide with any `feature_staging_<hive_identifier>` name; update the pipeline/node wiring that names these datasets (renderer-internal, `_check_wiring` will catch any miss) and regenerate the cif_daily golden. (e) `validation.py`: key `_read_set` by `(_fold(schema), _fold(table))`, fold the spine requirement the same way, and make the partition loop's `physical in denied` use folded tuples.

- [ ] **Step 4: Regenerate affected goldens, package green + commit**: `git commit -m "fix(materialize): render hygiene — YAML control chars, dotted schemas, dataset-name collisions, L1 case folding"`

---

## Phase F — test debt

### Task 24: Pin the hasher, and make identity components load-bearing in tests

Cross-plan coordination: the 2026-08-01 verified-interface ledger extracts the RFC-8785
implementation to a neutral shared contract hasher. If that extraction lands first, pin the same
vector through both entry points and require `materialize_hash` to delegate byte-identically; do not
retain a fork merely to preserve this module path.

`test_canonical.py`'s four assertions are all relative — a `json.dumps(sort_keys=True)` impostor passes every one, silently invalidating every sealed artifact. And `identity_payload` components (`aggregation`, `filter_tree`, join-step `cardinality`) can be deleted with the suite green because every hash assertion is `f(x) == f(x)`.

**Files:**
- Modify: `tests/featuregen/materialize/test_canonical.py`
- Modify: `tests/featuregen/materialize/test_expression_ir.py`

- [ ] **Step 1: Compute the pinned vector once** (this is the only step that runs before its test exists):

```bash
uv run python -c "
from featuregen.materialize.canonical import materialize_hash
print(materialize_hash({'b': [1, 2.5, 'é'], 'a': {'nested': {'y': 1, 'x': None}},
                        'n': 1234567890123456789}))"
```

- [ ] **Step 2: Add the tests**, pasting the digest:

```python
def test_pinned_rfc8785_vector():
    # A hasher that drifts from RFC 8785 (unicode escaping, float form, nested key
    # order) silently forks the identity of every sealed artifact. This digest was
    # computed at 28305084 and must never change.
    payload = {"b": [1, 2.5, "é"], "a": {"nested": {"y": 1, "x": None}},
               "n": 1234567890123456789}
    assert materialize_hash(payload) == "<paste the digest from Step 1>"

def test_nested_key_order_is_irrelevant_too():
    assert materialize_hash({"a": {"x": 1, "y": 2}}) == materialize_hash({"a": {"y": 2, "x": 1}})
```

- [ ] **Step 3: Identity-discrimination tests** in `test_expression_ir.py` (using its existing IR builders — the ones `test_the_same_expression_compiles_to_the_same_hash_twice` (:463) uses): three tests asserting `ir_hash` **differs** between two IRs that differ only in (a) aggregation (SUM vs COUNT_DISTINCT, same operand/window), (b) presence of a filter, (c) a join step's cardinality. Each carries a one-line comment naming the deletable-payload-component finding.

- [ ] **Step 4: Run all green** (they pass against the correct implementation — their value is what they now refuse to let regress) **+ commit**: `git commit -m "test(materialize): pin the RFC-8785 digest and make identity components load-bearing"`

### Task 25: fake_spark models NULL ordering, three-valued logic, and DATE literals

The stand-in raises `TypeError` where Spark sorts/drops NULLs (making the entire null-bearing half of banking data untestable), evaluates `!=`/`~`/`isin` with Python truthiness where Spark returns NULL (rows the cluster drops, the fake keeps), and `run_rendered`'s namespace lacks `date` so DATE-literal filters have zero behavioral coverage.

**Files:**
- Modify: `tests/featuregen/materialize/fake_spark.py` (:168-189 `_compare`, :201-203 `__invert__`, :307-310 `isin`, :427-437 ordering, :865-867 namespace)
- Test: fake_spark's own self-tests (`tests/featuregen/materialize/test_fixtures.py` or wherever `fake_spark` fidelity tests live — locate with `grep -rn "fake_spark" tests/featuregen/materialize/test_*.py | head`)

- [ ] **Step 1: Failing self-tests** (in the fake's fidelity test file):

```python
def test_null_ordering_matches_spark():
    # Spark: NULLS FIRST on asc, NULLS LAST on desc. The fake raised TypeError,
    # which made every null-timestamp scenario untestable (report §6).
    rows = [{"t": None}, {"t": 2}, {"t": 1}]
    assert [r["t"] for r in _sorted_partition(rows, [_asc("t")])] == [None, 1, 2]
    assert [r["t"] for r in _sorted_partition(rows, [_desc("t")])] == [2, 1, None]

def test_three_valued_logic_drops_null_comparisons():
    df = DataFrame([{"s": None}, {"s": "ok"}])
    assert [r["s"] for r in df.where(F.col("s") != "cancelled").collect()] == ["ok"]
    assert [r["s"] for r in df.where(~F.col("s").isin(["x"])).collect()] == ["ok"]

def test_date_literals_are_executable():
    node_source = 'def f(df):\n    return df.where(F.col("d") >= date(2026, 1, 1))\n'
    run_rendered(node_source, "f")     # old: NameError on date
```

- [ ] **Step 2: Run, verify FAIL** (TypeError / kept rows / NameError).

- [ ] **Step 3: Implement.** (a) Ordering: in `_sorted_partition` (:434-436), wrap the key: `key=lambda row, o=ordering: _null_last_key(o.column._eval(row))` where `_null_last_key(v) = (v is not None, 0 if v is None else v)` — with the existing `reverse=ordering.descending` this yields NULLS FIRST asc / NULLS LAST desc; same wrap in `_sort_key` (:427). (b) 3VL: in `_compare` (:168-171), return `None` when either side evaluates to `None` (before applying `op`); `__invert__` → `None if v is None else not v`; `isin` → `None if v is None else v in allowed`. `where` already drops rows whose predicate is falsy, so `None` rows drop — Spark's behavior. Keep `__eq__` on the same `_compare` path (Spark `==` is null-propagating too). (c) Namespace: add `"date": datetime.date` to `run_rendered`'s namespace (:865-867).

- [ ] **Step 4: Repair the fallout honestly.** Some existing tests will fail because they relied on the fake's non-SQL semantics — each one was silently testing the wrong thing. Fix the **test expectations** to Spark semantics (never re-permissive the fake); if a repaired expectation exposes a real renderer bug, fix it in this task and say so in the commit.

- [ ] **Step 5: Package green + commit**: `git commit -m "test(materialize): fake_spark models NULL ordering, 3VL and date literals — the null half of banking data is now testable"`

### Task 26: Dirty-data suite, golden guard, and joins coverage

The fixture monoculture (all-positive amounts, single-column keys, unique dimension rows, no NULL temporals) is what let §2's defects hide. Plus: goldens self-write when absent (delete → rerun → blessed), and `test_joins.py` only ever puts the bad hop last.

**Files:**
- Modify: `tests/featuregen/materialize/test_render_nodes_compute.py`, `test_spine.py`, `test_joins.py`, `test_render_project.py:909`
- Modify: the four golden self-write sites (`test_render_nodes_compute.py:556-561` pattern, four golden dirs)

- [ ] **Step 1: Golden guard.** Replace all four self-write sites with:

```python
    if not golden.exists():
        if os.environ.get("UPDATE_GOLDENS") != "1":
            pytest.fail(f"golden {golden} is missing — if this is an intended renderer "
                        f"change, regenerate with UPDATE_GOLDENS=1 and REVIEW the diff")
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(rendered, encoding="utf-8")
```

and fix `test_render_project.py:909`'s message, which points at a `_write_goldens` helper that does not exist — point it at `UPDATE_GOLDENS=1`.

- [ ] **Step 2: Dirty-data behavioral tests** (all runnable now that Task 25 landed; use `_customer`/`_txn`/`_debit`/`_run`):

```python
def test_a_null_event_time_row_is_excluded_from_every_window():
    # Spark drops NULL-predicate rows; the fake used to raise instead (report §6).
    rows = [_txn(), _txn(txn_dt=None)]
    ...assert the aggregate counts exactly the dated row...

def test_cancelling_debits_reach_the_zero_denominator_policy():
    # The realistic path to a zero denominator is cancellation, not a literal zero.
    rows = [_debit("C1", Decimal("50.00")), _debit("C1", Decimal("-50.00"))]
    ...ratio feature with zero_denominator=NULL -> published NULL, no gate fire...

def test_negative_boundary_overflow_is_caught():
    ...sum of large NEGATIVE decimals trips the Task-7 gate symmetrically...

def test_an_empty_spine_produces_an_empty_publish_and_no_gate_noise():
    ...zero rows after the status filter -> assembled empty, all gates pass...
```

And in `test_spine.py`, a composite-key spine test: `ordered_key_refs` with two governed key columns validates and renders a two-column select (every current fixture is a 1-tuple — `_refuse_wrong_entity`'s multi-key loop is unexercised).

- [ ] **Step 3: Joins first-hop coverage** in `test_joins.py`: clone the three bad-hop-last tests (:195, :228, :245) with the defect on hop 1 (a `steps[-1:]` mutation currently survives the file), and add an `"N:N"` token reaching `plan_join` end-to-end.

- [ ] **Step 4: Full suite green + commit**: `git commit -m "test(materialize): dirty-data suite, guarded goldens, first-hop join coverage"`

---

## Phase G — NOT in this plan: wiring the chain (separate brainstorm + plan)

This follow-on is now a **hard product predecessor**, not optional deferred polish: the semantic
plan's generated-project acceptance and profile Release C cannot claim execution until a reviewed
Phase-G plan is implemented. Completing Tasks 1–26 fixes the library/artifact but does not create a
production caller.

The review's №1 finding — the compile→render→L0/L1→submit→publish chain has zero production callers — is new product surface, not a defect fix, and per the standing prose-architecture preference it needs a design conversation before a plan. Open questions for that session:

1. **Surface**: an API route (`POST /features/{id}/materialize`?) vs. a durable-queue worker job (the `recipe_formula_shadow` pattern) — and which RBAC role may trigger it.
2. **Gate policy**: L0 mandatory pre-publish (cheap, this plan puts it in CI); is L1 (live metastore) blocking, and where does its cluster credential come from?
3. **Dry-run**: is a sampled/limited first execution required before `PUBLISHED`, and who reviews it?
4. **Evidence consumption**: score `recipe_formula_shadow_observation` against `GOLD_GATE_V1` on the planner's gate machinery (`gate_operate.py` is the shape to copy); wire `record_gap`/attempt-memory so refusals inform proposals.
5. **data_agent unification**: the live binding path never populates `partition_columns` (full-scan probes; pruning impossible) and the two binding paths fork `physical_id` — fixing either changes stored identity and needs a reconciliation decision.
6. **True read-scoping**: rendering partition predicates onto raw sources would make `input_snapshots` an enforced scope (closes the Task-21 deferral).

---

## Self-review notes

- **Coverage against the report**: §2.1→T6, §2.2→T4+T5, §2.3→T9, §2.4→T7, §2.5→T1+T3, §2.6→T8, §2.7→T2, §2.8→already in DEFERRED-WORK A.20 (no task, by design), §3.1→T10+T11, §3.2→T13, §3.3→T12, §3.4 (bridge-gate two-projection bypass, PLAUSIBLE)→**deliberately deferred to Phase G** (only reachable once a caller can assemble two projections; verify during wiring), §4→T14-T18, §5→T19-T23, §6→T24-T26. Wiring/§1 and the data_agent product changes→Phase G.
- Line numbers verified at `28305084` (both scout packs; zero drift from the review commit). Helper names inside test files (`_render_files`, `_traversal_node`, `_active_population_spine`, etc.) are **adapt-to-file** placeholders by intention — each task says to reuse that file's real builders; inventing parallel fixtures is the failure mode to avoid.
- Migration 1044 is reserved for this plan's Task 15; shared 1045–1049 reservations live in the
  verified-interfaces ledger.

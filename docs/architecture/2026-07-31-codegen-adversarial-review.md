# Adversarial review — Kedro/PySpark code generation (2026-07-31)

**Target:** `src/featuregen/materialize/` (+ formula authoring, data_agent integration, API wiring) at `origin/main` `a2203bc9`.
**Method:** 6 parallel adversarial finder agents (IR/identity, compile/planning, render, execution/wiring, LLM boundary, test adequacy), then 4 adversarial verification agents instructed to *refute* the top claims. Spark/Kedro semantics claims were verified **empirically** against PySpark 4.2.0 (`.venv-l0`, ANSI off to emulate 3.5.1) and kedro 0.19.9/1.5.0.
**Labels:** CONFIRMED = survived a dedicated refutation pass (or was reproduced by execution). FINDER-VERIFIED = the finder self-refuted and cited evidence, but no second pass ran. PLAUSIBLE = single-source, mechanism credible, not independently checked.

---

## Executive verdict

The materialize subsystem is the most carefully engineered code in the platform — one hasher, vendored vector-pinned JCS, closed refusal vocabularies enforced at construction, a deliberately strict fake-Spark stand-in, and an LLM trust boundary that genuinely prevents model text from reaching emitted code. **And it is not yet a product feature:** the entire compile→render→validate→submit→publish chain has zero production callers (the §2 orchestrator is explicitly not built), so none of its protections has ever run against real governed input.

Beneath that, verification confirmed a cluster of **silent-wrong-number defects with a single root cause: the renderer reasons about Spark types the IR never carries** — no physical dtype for the event-time clock, no model of the intermediate types Spark derives for `sum`/`divide` before the emitted cast/`bround` runs. Determinism is not correctness: the renderer emits the same wrong code every time, and the missing "critic" is not another LLM — it is the executable verification (L0/L1/dry-run) that already exists and is unwired.

---

## 1. The delivery gap (CONFIRMED twice, independently)

Every entry point past the formula layer is an orphan: no API route, CLI command, or worker handler reaches `admit_artifacts`, `compile_ir`, `authorize_compilation`, `build_group_plan`, `render_project`, `run_l0/l1`, `LocalClusterSubmitter.submit`, `select_publisher`, or any control-plane writer. The only live cross-module imports are `canonical.materialize_hash`, `codes.MaterializationRefused`, `inventory.ClusterInventoryV1` (as a type), and `inputs.derive_requirement` (reachable only via an orphaned chain). Migration 1034's seven control-plane tables can never receive a production row. `compile/__init__.py` states it: the §2 orchestrator is not built.

Consequences that compound it:

- **The inventory-aware binding path is the dead one.** The live path (`binding_store.resolve_table/resolve_binding` → `analysis/assembly.py`) builds bindings with empty `partition_columns`, so the `NO_PARTITION_SELECTOR` guard can never fire, every relationship probe is a predicate-free scan, and supplying a selector raises `UNKNOWN_PARTITION_COLUMN`. Partition pruning is structurally impossible on the production path. (FINDER-VERIFIED)
- **Two binding paths fork `physical_id`** (`database` = `environment_id` vs `connection_id` vs stored column) — the exact identity split `physical.py`'s docstring forbids; profiles/observations for one table never reconcile. (FINDER-VERIFIED)
- Shadow/LLM evidence is write-only: `recipe_formula_shadow_observation` is populated but `recipe_formula_gate.py` / `recipe_formula_eval.py` have no callers; `GOLD_GATE_V1` is offline; `learning.record_gap` is reachable only via `run_analysis`, which has no src caller, so `GET /learning/gaps` reads a table nothing populates.

## 2. Confirmed correctness defects (silent wrong numbers)

**2.1 CRITICAL — `ActivePopulation` spine with `availability_ref=None` has no point-in-time filter anywhere.** (CONFIRMED by execution)
`spine.py:493-506` imposes no availability requirement on this policy (unlike all three siblings); `_refuse_ungoverned_availability` short-circuits on `None`; the rendered `build_spine` is literally `rows = source.where(status.isin([...]))` with `business_dt` used only to stamp the output column. `runprep.spine_input_request` never mentions `ActivePopulation` — and `test_runprep.py:929` *blesses* resolution for an arbitrary business date. A January backfill run in July silently uses July's active-customer set: population-level label leakage, no gate fires. **This is a spec-level hole faithfully implemented** (`availability_ref: str | None` optional in Spec A). Fix: a `CurrentSnapshot`-style vintage refusal — merely requiring `availability_ref` only half-closes it because `status_cd` is current-valued (customers closed since the backfill date still vanish).

**2.2 CRITICAL — same-catalog traversal hops have no runtime fan-out gate.** (CONFIRMED; found independently by two agents)
`nodes_compute.py:1668-1681` emits the `JOIN_AMPLIFICATION` row-count guard only for `hop.directional` (cross-catalog bridges); bridges additionally get a pre-join target-uniqueness gate (`nodes_join_gate.py:136-145`). Ordinary hops get neither. A duplicated dimension key (double-loaded dim, SCD2 without a current-row filter) doubles every fact row and every SUM; the spine reduction then collapses to one row per key so `KEY_NOT_UNIQUE` passes, and **every manifest count is grain-level, taken after the collapse — nothing a human or gate looks at moves**. `dropDuplicates` is deliberately never emitted. Verification traced the trusted cardinality to its origin and it got worse: `overlay/upload/graph.py:80-93` fills a blank uploaded cardinality with a **default `"N:1"`**, which two admins then rubber-stamp into a VERIFIED fact runtime trusts absolutely. Not in DEFERRED-WORK.md. Fix is cheap: reuse the `nodes_join_gate` target-uniqueness form for same-catalog hops (or count-guard every hop).

**2.3 CRITICAL (for west-of-UTC zones) — DATE-typed event clock shifts the whole window by a day.** (CONFIRMED empirically)
The renderer emits `F.to_utc_timestamp(boundary.cast('timestamp'), zone)` and compares the event-time column against it (`nodes_compute.py:1838-1846`). If the clock column is Hive `DATE`, Spark casts it to midnight UTC; for `America/New_York` the measured result **drops the entire first day of the window and admits an extra trailing day**. East-of-UTC zones are correct by luck (the goldens' zone hides it). Root cause: `PitSpec.event_time_ref` is a bare `str` — the IR carries no physical type, so the renderer *cannot* know to compare date-to-date.

**2.4 HIGH — aggregate overflow publishes as NULL; `overflow=error` is unreachable for the dominant path.** (CONFIRMED empirically)
The emitted gate is `staged.where(F.col(c).isNotNull() & typed.isNull())` — but Spark's `CheckOverflowInSum` yields NULL *before* the gate for a sum exceeding its own result type (measured: `sum(DECIMAL(38,2))` of 2×9.99e35 → `None`, gate flags 0 rows). A genuine overflow publishes as NULL, indistinguishable from an empty window. Related amplifier (PLAUSIBLE): `null_input: zero` coalesces the operand up to the published `DECIMAL(38,6)` before summing, destroying Spark's p+10 headroom, making this far more likely than under `ignore`.

**2.5 CRITICAL (availability) — the rendered project cannot run under its own pinned kedro.** (CONFIRMED empirically, from installed kedro source)
`RunParametersHook` reads `run_params.get("runtime_params")` (`project.py:775`), but kedro **0.19.9** — the golden's `requirements.lock` pin — passes the key as `extra_params`; `runtime_params` exists only in kedro 1.x. So `supplied = set()` and every run refuses with `RUN_PARAMETERS_MISSING`. Fail-closed, but the artifact is structurally unable to execute, and the L0 gate cannot catch it: it only *builds* the pipeline, and does so under `.venv-l0`'s kedro 1.5.0, where the key happens to be right. (The config-loader resolver `${runtime_params:...}` *is* correct on 0.19.x — the renderer generalized that name onto the hook payload.)

**2.6 MEDIUM — declared `half_even` rounding is silently `half_up` at ties for ratios.** (CONFIRMED empirically)
Spark's decimal `Divide` applies `CheckOverflow` with hard-coded HALF_UP at the division result scale (clamped to `MINIMUM_ADJUSTED_SCALE=6`) before the emitted `F.bround` runs; at published scale 6 the bround is a no-op (measured: 1/2000000 → 0.000001, not the HALF_EVEN 0.000000). At other scales the bug changes shape (double rounding, or silent digit loss) rather than disappearing.

**2.7 MEDIUM-HIGH — ANSI mode is unpinned, and the L0 validator runs under the opposite default.** (CONFIRMED)
`spark.yml` pins only app name and session timezone. The overflow gate and coalesce shapes are correct only under ANSI-off (the 3.5.1 default); under ANSI-on the cast **raises** `NUMERIC_VALUE_OUT_OF_RANGE` — a raw Py4J failure outside the closed gate-code vocabulary (measured on 4.2.0, where ANSI defaults **true**). A bank cluster's `spark-defaults.conf` silently changes governed semantics. Fix: pin `spark.sql.ansi.enabled: false` in the rendered `spark.yml` (one line), or make the gates ANSI-safe.

**2.8 KNOWN (documented deferral, not a new finding) — traversal dimensions are read as-of run time.** Behavior confirmed (`_bridge_predicates` populates hop predicates only for cross-catalog; the two-hop golden joins raw `SparkHiveDataset` dims unfiltered), but `docs/DEFERRED-WORK.md` A.20 states it verbatim as a red deferral with an explicit gate: fix before any multi-hop feature is used for training or backtests. Kept here for completeness because it composes with 2.1 into end-to-end PIT unsoundness for backfills.

## 3. Governance/authorization defects

**3.1 HIGH (latent) — Gate 2 misses the migration-1032 read-scope contract.** (CONFIRMED as drift; latent because unwired)
`ir.py:531` and `spine.py:559/576` authorize against raw `graph_node.sensitivity` only. Migration 1032 (2026-07-28) made `visible_requires <@ %s` the read-scope predicate and converted 18 files — zero under `materialize/`. `classify.py` refuses only `prohibited`; `restricted`/`confidential` become labels. The migration's own header measures the exposure: 28/126 FTR columns (names, addresses, phones, an Emirates ID) with `sensitivity=NULL`, readable with no reader role. Compounding: `classify.py:163-176` builds `access_requirements` from the tag axis only, so the published contract carries an **empty** requirement tuple for exactly those columns (`RESTRICTION_ROLES` never consulted). The smoking gun: `read_scope.py:87` rebound `allowed_sensitivities = allowed_classes` (both vocabularies), so materialize now compares a post-1032 allowed-list against the pre-1032 column, while its docstring still claims the rule is "inherited rather than re-implemented". Fix: swap both queries to `read_scope.visibility_predicate()` + consult `RESTRICTION_ROLES` in classify; add the `restricted`/`confidential` test cases that don't exist.

**3.2 IMPORTANT — `LatestAvailableAsOf.effective_time_ref` is never checked for governance, contradicting its own docstring.** (FINDER-VERIFIED) A load-timestamp column can silently decide which record version wins, moving every joined attribute. The catalog has no `is_effective_time` fact to check against — either add one or record the gap; what's wrong today is asserting a check that isn't performed (`spine.py:172-174` vs `:513-527`).

**3.3 IMPORTANT — column existence is never proven before render.** (CONFIRMED by two agents) A ref with no `graph_node` row is treated as untagged-and-authorized (`ir.py:517-521`, deliberate), and `_read_column`'s membership check is against a read set derived from the formula itself. A hallucinated column in a real table renders cleanly and dies only at L1 — which is unwired. Cheap fix: a catalog-existence assertion at compile, or wiring L1 before publish. The recipe path is protected (operand pinned exactly by the expectation validator); any future free-authoring path is not.

**3.4 PLAUSIBLE — bridge-gate wiring bypass with two projections.** `project.py:619-624` requires the join-gate output to be read by *some* node, not by the projections rendering that realization's hop; with two projections one can join the unchecked raw dimension. Single-source; the only existing test uses one projection. Verify when wiring.

## 4. Latent execution-layer defects (for when it gets wired) — all FINDER-VERIFIED

- **Submitter timeout is not a bound** (`submit.py:152-161`): after `TimeoutExpired`, CPython re-enters `communicate()` with no timeout; a JVM grandchild holding the inherited pipes blocks forever, and only the direct child is killed (no process group), so a cluster-mode app keeps writing into `staging_root` while a retry becomes a second writer. Fix: `start_new_session=True` + group kill + bounded drain.
- **Control plane can be bricked** (`control_plane.py:387-399` + migration 1034): nothing prevents appending a non-terminal event after a terminal one; `fold_run_status` then raises forever, and the append-only trigger means no repair is possible. The two readers (`run_status` vs `published_generation_ids`) disagree about the same rows. Fix: a DB-level guard (trigger or exclusion) mirroring the fold's rule.
- **Stale attestation wins** (`publish.py:621-644`): an old passing probe overrides a newer failing probe on identical engine versions, contradicting the documented `PUBLISH_MECHANISM_UNSUPPORTED` split. Fix: newest-evidence-wins per version tuple.
- **Validation reports are write-only**: nothing SELECTs `pipeline_validation_report`, so `may_regenerate`'s blocking rule holds only in-process; the classification lives in a JSONB blob with no column/index. The "GOVERNED_FACT_MISMATCH blocks regeneration" property is unenforceable across processes as shipped.
- **Failure observations are unpersistable on Postgres** (`executor.py:97-108`): the probe's exception aborts the borrowed transaction; the carefully built failure observation then hits `InFailedSqlTransaction` at the store's first INSERT. No SAVEPOINT exists anywhere in `data_agent/`/`materialize/`.
- **L0's verdict channel is forgeable/fail-open** (`validation.py:409/450`): a generated project printing `@@L0-VERDICT@@` shapes its own verdict (the docstring names the attack; zero tests), `timeout_seconds` untested, and `except UnicodeDecodeError: return None` makes a non-UTF-8 file invisible to `PROJECT_HASH_MISMATCH`.
- Minor but real: stderr-tail-only diagnostics lose the traceback (`submit.py:171`); `PYSPARK_PYTHON`/`PYSPARK_DRIVER_PYTHON` documented mandatory, never checked; L1 case-folding inconsistency double-reports and un-suppresses partition findings; `input_snapshots` is required by the hook but consumed by no node, so `sandbox_execution_hash` names an *intended* read, not the performed one (§3.4's property is currently vacuous); raw `business_dt` string (unstripped) forks `sandbox_execution_hash`; backslash-terminated literal escapes the closing quote in Hive SQL (`_literal`, latent until `PartitionSelector` gets a caller).

## 5. IR/identity + data-model nits

- **SUM with `null_input=IGNORE` typed NOT NULL, one case short of Spark** (`physical_types.py:314`): an all-NULL non-empty window returns NULL; the renderer deliberately does not coalesce it (policy comment says so), so `WRONG_NULLABILITY` fires — a spurious, data-triggered refusal of a correctly authored feature. A test pins the wrong answer (`test_physical_types.py:575`). Fails closed, but wrongly.
- **Mixed-case logical refs fork `ir_hash`** (`expression_ir.py:727` vs `_table_ref_of`): one governed formula (hashes are case-folded) yields different `ir_hash` + duplicated `PhysicalInputRequirement` depending on ref spelling; trips `IR_HASH_MISMATCH` against an unchanged computation. Normalize refs at IR construction.
- `derive_group_contract` keys by `feature_name`, silently collapsing two IRs with one name across batches (bypasses `MULTIPLE_MATERIALIZATION_CONTRACTS`); `canonical.materialize_hash` accepts a narrower type than `filter_tree`'s annotation admits (`MappingProxyType` raises from inside a hash); YAML escaping covers only `\` and `"` (a newline in a table name folds to a *different table name* silently; catalog comments unescaped); `schema.split(".", 1)` mis-splits dotted schemas; `staging`/`manifest_` dataset namespaces collide with legal feature names (loud, but misnamed error).

## 6. Test suite & fake-Spark (the mutation-survival findings)

The suite is well above average — behavioral execution of rendered code against a deliberately strict stand-in, typed refusal codes, and the "what reading the goldens changed" commits are real golden hygiene. Its rigor is unevenly distributed: excellent at proving a declared policy was *rendered*, weak at proving the rendered code survives dirty data.

- **`test_canonical.py` has no pinned digest vector** — replacing RFC 8785 with `json.dumps(sort_keys=True)` passes all four tests; nested key order untested. Same across the suite: no pinned vector for `generated_project_hash`/`sandbox_execution_hash`, and `GENERATED.lock` is excluded from the golden comparison. One pinned-digest test each would close the "forked identity" hazard the package docstring names.
- **Identity payloads are mutable with the suite green**: deleting `aggregation`, `filter_tree`, or `join_steps[].cardinality` from `identity_payload` survives — all hash assertions are relative (`f(x)==f(x)`).
- **fake_spark cannot represent NULL in ordering/comparison columns** (raises `TypeError` where Spark sorts/drops), so the entire null-bearing half of banking data is *untestable*: null as-of timestamps, null-vs-null rank ties, NULL-dropping predicates. Also unmodelled: decimal intermediate type promotion (findings 2.4/2.6 were invisible by construction), three-valued logic for `!=`/`NOT IN` (fake keeps rows Spark drops), DATE filter literals (`run_rendered` provides no `date` binding → `NameError`, so zero behavioral coverage).
- **Fixture monoculture**: every amount positive, every grain key single-column, every ref lower-case, every dimension key unique, no DST/leap/negative/net-zero-denominator/composite-key/empty-spine case. The missing-case table in the full agent reports maps each absence to the module it leaves unverified.
- Goldens self-write when absent (delete → re-run → blessed). The human-review commits demonstrably caught prose defects, never a value defect. `test_render_project.py:909` points to a `_write_goldens` helper that does not exist.
- `test_joins.py` puts every bad hop last — a `steps[-1:]` mutation survives; `plan_cross_catalog_join` has zero tests in the file and never reads `cardinality_basis` (defense-in-depth gap given store rehydration).

## 7. The LLM boundary and the agentic-loop question

**Can LLM output corrupt generated code? No** — and this held up under a dedicated attack pass:

- The LLM authors a typed proposal parsed by a Draft-2020-12 schema with `additionalProperties:false` everywhere, into frozen slotted dataclasses; every enum is a closed vocabulary; `TypedLiteral` values are regex-parsed then re-parsed into typed Python at render; refs are emitted only inside `repr()`'d literals; `_safe_text` (24 call sites) rejects quote/backslash/newline in prose positions; the one free-prose field (`expected_output`) is **dropped** before the formula is sealed; the feature name is NFKC-folded to `^[a-z][a-z0-9_]{0,127}$` with collisions refused — and in the wired path it isn't LLM text at all (`recipe_id`).
- Recipe lockdown pins aggregation/operand/relation/event-time/window/grain/decimal policy to the deterministic expectation and rejects any LLM-authored filter or parameter outright.
- Gate 1 re-derives every hash from the WORM trace, never trusting the artifact's own fields. Critic severity comes from a fixed map, not the model. Tool results ride `tool_trail`, never concatenated into instruction text (prompt-injection defense).
- Real gaps at this boundary: referent truth (3.3 — shape is validated, existence is not), `authoring_intent_hash` not covering `recipe_authoring_context`, `_read_c1_facts` unscoped, free-text names checked for Hive-expressibility only at the last mile (validate at acceptance with the same public `hive_identifier`).

**Do we have a proper ReAct/critic loop?** For *formula authoring*, yes — and it is the right shape: the author **is** a ReAct agent (8 bounded turns, 7 read-only tools), reviewed by an **independent** LLM critic with separately assembled context and fail-closed folding, with bounded structural-repair budgets (2+2) below it, and a WORM trace above it. For *code generation*, the correct answer is that a critic agent over the emitted PySpark is **largely obviated by the architecture**: the renderer is deterministic, sealed by hash, and behaviorally tested — an LLM opinion adds nothing a hash and an executable harness don't. The world-class gap is not another model in the loop; it is that **the loop never closes**:

1. **No orchestrator** (§2): the chain can't run end-to-end, so no protection has ever fired in production. Everything else is downstream of this.
2. **L0 build verification exists and runs nowhere** — not in CI (`l0_gate.py` is deliberately uncollected), not in any pipeline. Finding 2.5 (kedro key) is the proof of what that costs: the artifact can't run and nothing knows. Put L0 (under the *pinned* engine versions, not the dev venv) in CI today.
3. **Execution evidence is never consumed**: shadow observations write-only, gold gate offline, `record_gap` unreachable, `attempt_memory` caller-less, validation reports unreadable. The planner already has the full shadow→gold-suite→gate→enable machine (`api/routes/gate.py` + `gate_operate.py`) — copy that shape for the formula/codegen loop.
4. **No sample-data dry run before publish** — the 18 rendered runtime gates convert most wrongness into aborted runs, but (2.2, 2.4) show the two classes they miss: fan-out and overflow-as-NULL. A limited-scope first run (or differential rendering across renderer versions — mechanical, not agentic) covers the residue.
5. **"100% accurate" needs the IR to carry physical types**: 2.3/2.4/2.6 share one root cause — the renderer cannot reason about Spark's actual types. Carry the clock column's dtype and model the intermediate aggregate/division types, then the renderer can emit date-typed comparisons, pre-widened accumulators, and honest rounding.

## 8. Recommended order of work

| # | Action | Closes |
|---|--------|--------|
| 1 | Pin `spark.sql.ansi.enabled` in rendered `spark.yml`; fix the kedro hook key (emit per pinned kedro major, or read both keys); run L0 in CI under the pinned engines | 2.5, 2.7 |
| 2 | Add the same-catalog hop uniqueness/count gate (reuse `nodes_join_gate` form); stop defaulting blank cardinality to `N:1` | 2.2 |
| 3 | `ActivePopulation` vintage refusal in `runprep.spine_input_request` (and reconsider the spec's optional `availability_ref`) | 2.1 |
| 4 | Swap Gate-2/spine read-scope to `visibility_predicate()` + `allowed_classes`; consult `RESTRICTION_ROLES` in classify | 3.1 |
| 5 | Carry the event-clock physical type in `PitSpec`; emit date-typed comparisons; model intermediate sum/divide types (pre-widen or post-check honestly); fix the overflow gate premise | 2.3, 2.4, 2.6 |
| 6 | Wire the §2 orchestrator behind a flag, with L0+L1 mandatory before publish and a sampled dry run | §1, 3.3, D4 |
| 7 | Close the loop: score shadow observations against `GOLD_GATE_V1` on a schedule (copy the planner's gate machine); wire `record_gap`/attempt-memory | D6, D7 |
| 8 | Execution-layer hardening batch: process-group kill + bounded drain; DB guard on post-terminal events; newest-attestation-wins; a reader (and column) for validation reports; SAVEPOINT around probes | §4 |
| 9 | Test-debt batch: pinned digest vectors; null-capable fake_spark ordering/3VL; negative amounts; composite keys; duplicate dim keys; identity-payload deletion tests | §6 |

**Process note for future reviews** (from the verification wave): two finder Criticals were already in `docs/DEFERRED-WORK.md` A.20 — reviewers must read the deferral ledger first; and the fan-out finding only got its true shape by tracing the governed fact to its *provenance* (`graph.py:93`'s blank→`N:1` default), not stopping at the gate that trusts it.

---

*Full per-agent reports (finders + verifiers) are in the session transcripts; this document keeps the deduplicated, verification-labeled findings.*

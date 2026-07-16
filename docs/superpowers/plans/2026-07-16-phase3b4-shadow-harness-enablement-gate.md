# Phase 3B.4 — Shadow Harness + Objective 3C Enablement Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`). **D3 (dual input hashes) + D8 (statistical bound + detached-signed gate) are the highest-scrutiny tasks.** Shadow/telemetry-gated; nothing enforces.

**Goal:** Persist each 3B.3c contract classification (manifest + run + plan, WORM), fingerprint it for replay, cause-label the population, and produce the objective, detached-signed **conjunctive gate** that governs 3C enablement.

**Architecture:** Migration `0996` + three append-only tables + `planner/shadow_store.py`; a separate telemetry flag; a dispatch manifest written before scope resolution; two inputs-only hashes; `ReplayFreshness`; a two-layer cause taxonomy; a curated gold set + `contract_eval`; a population report + a 7-gate signed-artifact evaluator. Plus three merged-3B.3c defect folds.

**Tech Stack:** Python 3.11 (frozen dataclasses, StrEnum), PostgreSQL (append-only/WORM), pytest (`db` fixture). `uv run pytest/ruff/mypy`. **Spec:** `docs/superpowers/specs/2026-07-16-phase3b4-shadow-harness-enablement-gate-design.md` (v3).

## Global Constraints (every task)

- **Shadow / telemetry-gated / behaviour-neutral.** Persistence is gated by a NEW `FEATUREGEN_INTENT_SHADOW_TELEMETRY` flag (separate from the compile flag). Both flags off → **zero writes, response byte-identical**. The live grounding path + route response are never affected; the flags are read ONLY in the route (planner stays pure — no `os.environ` in `planner/`). Full `tests/featuregen` green.
- **Append-only / WORM.** All three tables `REVOKE UPDATE, DELETE, TRUNCATE FROM featuregen_app` (mirror `0971`/`0974`); composite PKs; FKs; DB CHECK constraints on every status enum; idempotent inserts (`ON CONFLICT` reconcile, divergent-duplicate = validated conflict); canonical sorted-key JSON + `payload_schema_version` + payload hash. Key on `generation_run_id` (never `run_id`).
- **Capture integrity is provable.** The manifest (the *expected* set) is written FIRST, before `resolve_catalog_scope`, WITHOUT `catalog_scope_id`; a pre-loop failure is caught inside `run_shadow_planner` (returns normally) so the route savepoint retains the manifest. The loss signal is manifest↔results reconciliation + an external metric — never a circular self-report.
- **Hashes are INPUTS-ONLY.** `planner_input_hash` (full candidate universe → selection stability) and `contract_input_hash` (selected plan → verdict stability) hash pre-classification state only; declarations/verdicts are OUTPUTS, hashed separately.
- **The gate is conjunctive** (every sub-gate; no averaging), **detached-signed** (the evaluator cannot sign its own output), **human cannot override a FAILED machine gate**, **nonzero exit on failure**.
- **Convention:** frozen `@dataclass(frozen=True, slots=True)`; lowercase-snake StrEnum; ruff (`collections.abc`, E402; scope ruff to touched files — ~34 pre-existing `passc/` errors are a parallel session's); no import cycle (`planner/*` must not import the route). Branch `feature/phase3b4-shadow-harness`; harness default trailer.

## Reused interfaces (verified)
- Migrations: `src/featuregen/db/migrations/NNNN_*.sql` (runner auto-discovers; confirm current max + 1 — spec says `0996`). WORM/CHECK/`ON DELETE CASCADE`/`DO $$ … REVOKE … FROM featuregen_app … $$` pattern from `0974_intent_scope_records.sql`. Append-only write = `conn.execute("INSERT INTO … ", (…))` (see `contract/scope_records.py`).
- Shadow entry: `run_shadow_planner(conn, *, eligible_recipe_ids: frozenset[str], target_entity, roles=(), run_id, now, compile_contracts=False, templates=None)` (`shadow.py`); per-recipe `with conn.transaction()` savepoint. Route call at `contract.py` inside `if body.catalog_source is None and scope.target_entity is not None:` — `applicability.eligible_ids`, `generation_run_id`, `applicability`'s version, `identity.role_claims`, `now` all in scope; the compile flag is read here (`os.environ.get("FEATUREGEN_INTENT_CONTRACT_COMPILE","0")=="1"`).
- `build_compiler_context(conn, scope, roles, now) -> CompilerContext` (has `catalog_fingerprint_at_start`/`bridge_fingerprint_at_start`/`columns_by_catalog`); `_load_columns(conn, catalog, roles) -> list[_Col]` (`_Col.additivity/is_as_of/entity/sensitivity/concept/is_grain/data_type`); `resolve_catalog_scope`.
- Budget seam: `CompileBudget(remaining, deadline)`; the deadline check `budget.remaining > 0 and compile_ctx.now < budget.deadline` (`plan.py:206`) — inert wall-time (F12). `MAX_COMPILES_PER_RUN=500`, `COMPILE_BUDGET=timedelta(30s)` (`shadow.py`).
- 3B.3c contracts: `BindingPlanningResultV1(run_id, recipe_id, catalog_scope_id, target_entity, result_status, contract_result_status, selected_contract_physical_plan_id, selected_contract_id, candidate_plans, ...)`; `BindingPlanV1(physical_plan_id, contract_id, path_resolution_status, contract_resolution_status, declaration_status, contract_primary_reason_code, contract_reason_codes, hop_aggregations, temporal_declaration, physical_read_set, audit_envelope, ...)`; `PlannerReplayEnvelopeV1.active_bridge_fact_keys`/version set; `CatalogStateStampV1(catalog_source, head_seq, last_completed_at, stamp_kind)`; `ReplayStrength.audit_only`; `ReasonCode` (the full registry); `ROLE_RESOLUTION_VERSION="unknown"` (contracts.py:37 — fix). `drift_watermark`/`drift_head_seq`/`_checkpoint_seq`/`realization_fingerprint`.
- take_latest: `compile_temporal` sets `pit_anchor` even when `anchor_binding is None` (`declarations.py:268`); `_validate_stage` (`declarations.py:439`); connectivity `placement` (need_role → PathPosition).

## File Structure

| File | Responsibility |
|---|---|
| `db/migrations/0996_planner_shadow_store.sql` (CREATE) — D1 | the 3 WORM tables |
| `planner/shadow_store.py` (CREATE) — D1/D2 | store contracts, enums, two-phase writer, readers, reconciliation |
| `planner/shadow.py`, `api/routes/contract.py` (MODIFY) — D2 | telemetry flag, manifest-first, nested savepoint, capture wiring |
| `planner/fingerprint.py` (CREATE) — D3 | `planner_input_hash` + `contract_input_hash` (inputs-only) + stamp enrichment |
| `planner/replay.py` (CREATE) — D4 | `ReplayFreshness` (pure comparator + impure adapter) |
| `planner/cause.py` (CREATE) — D5 | Layer-A map (exhaustive) + Layer-B contextual + `ResolutionCause` |
| `planner/declarations.py`, `plan.py`, `shadow.py`, `contracts.py` (MODIFY) — D6 | compile_status, monotonic budget, take_latest stage-local |
| `planner/contract_gold.py` + `contract_eval.py` (CREATE) — D7 | gold set + evaluator + audit sampler |
| `planner/shadow_report.py` (CREATE) — D8 | population report + 7-gate + signed artifact + verifier |
| Tests | `tests/featuregen/overlay/upload/planner/test_shadow_*.py` + route/migration tests |

---

### Task D1: Migration 0996 + store contracts + two-phase writer

**Files:** Create `db/migrations/0996_planner_shadow_store.sql`, `planner/shadow_store.py`; Test `test_shadow_store.py`.

**Produces:** the 3 tables; `PlannerOutcome`/`CompileStatus`/`IncompleteReason`/`CaptureStatus` enums; `DispatchRecordV1`/`RunResultRowV1`/`PlanObservationRowV1`; `write_dispatch`/`write_run_and_plans`/readers; `reconcile(run_id) -> ReconcileResultV1`; canonical JSON + payload hash.

- [ ] **Step 1: confirm the migration number** — `ls src/featuregen/db/migrations/ | grep -oE '^[0-9]{4}' | sort -n | tail -1` → use max+1 (spec: `0996`).
- [ ] **Step 2: write the migration** — three `CREATE TABLE IF NOT EXISTS` (mirror `0974` style + comment header):
  - `planner_shadow_dispatch(generation_run_id text PRIMARY KEY, eligible_recipe_ids text[] NOT NULL, recipe_hash text NOT NULL, expected_count int NOT NULL, invocation_predicate text NOT NULL, compile_flag bool NOT NULL, telemetry_flag bool NOT NULL, applicability_version text NOT NULL, producer_commit text NOT NULL, compiler_versions jsonb NOT NULL, payload_schema_version text NOT NULL, created_at timestamptz NOT NULL)`.
  - `planner_shadow_run_result(generation_run_id text NOT NULL REFERENCES planner_shadow_dispatch(generation_run_id) ON DELETE CASCADE, recipe_id text NOT NULL, catalog_scope_id text, planner_outcome text NOT NULL CHECK (planner_outcome IN ('compiled','no_physical_plan','internal_error','no_authorized_catalog','template_not_found','preloop_failure')), compile_status text NOT NULL CHECK (compile_status IN ('complete','incomplete','not_applicable')), incomplete_reason text CHECK (incomplete_reason IN ('budget_count','budget_time','error')), path_resolved_eligible int NOT NULL, compiled_count int NOT NULL, skipped_count int NOT NULL, capture_status text NOT NULL CHECK (capture_status IN ('persisted','persistence_partial')), selected_contract_physical_plan_id text, selected_contract_id text, contract_result_status text, payload_schema_version text NOT NULL, created_at timestamptz NOT NULL, PRIMARY KEY (generation_run_id, recipe_id))`.
  - `planner_shadow_plan_observation(generation_run_id text NOT NULL, recipe_id text NOT NULL, physical_plan_id text NOT NULL, contract_id text, contract_input_hash text NOT NULL, path_resolution_status text NOT NULL, contract_resolution_status text NOT NULL, declaration_status text NOT NULL, contract_primary_reason_code text, contract_reason_codes text[] NOT NULL, bridge_count int NOT NULL, tier text NOT NULL, preference_rank int NOT NULL, declarations jsonb NOT NULL, declarations_output_hash text NOT NULL, replay_stamp jsonb NOT NULL, payload_schema_version text NOT NULL, created_at timestamptz NOT NULL, PRIMARY KEY (generation_run_id, recipe_id, physical_plan_id), FOREIGN KEY (generation_run_id, recipe_id) REFERENCES planner_shadow_run_result (generation_run_id, recipe_id) ON DELETE CASCADE)`.
  - Indexes on `(generation_run_id)`, `(recipe_id)`, `(contract_input_hash)`; the WORM `DO $$ … REVOKE UPDATE, DELETE, TRUNCATE ON <each> FROM featuregen_app … $$`. **`is_selected` is DERIVED** (a read-time join to `run_result.selected_contract_physical_plan_id`), never a column.
- [ ] **Step 3: store contracts + writer (`shadow_store.py`)** — the enums + row dataclasses; `_canonical_json(obj)` (sorted keys) + `_payload_hash`; `write_dispatch(conn, DispatchRecordV1)`; **`write_run_and_plans(conn, run_result, observations) -> CaptureStatus`** — the **two-phase protocol (F11)**: attempt an ATOMIC `with conn.transaction(): insert run_result + all observations`; on failure roll back, then a SECOND `with conn.transaction(): insert run_result ONLY with capture_status='persistence_partial'`; if that raises, re-raise (the caller's manifest reconciliation + external signal handle it). Idempotent inserts (`ON CONFLICT (…) DO NOTHING` + a divergent-duplicate validation read → raise on mismatch). `reconcile(conn, run_id) -> ReconcileResultV1(expected, present, missing_recipe_ids)`.
- [ ] **Step 4: tests** — round-trip; WORM (a test asserts UPDATE/DELETE/TRUNCATE are revoked — or, in the superuser test cluster, that the migration ran); idempotent re-insert = no-op; divergent duplicate → conflict; two-phase: monkeypatch the child insert to fail → `persistence_partial` parent appears; reconcile detects a missing recipe.
- [ ] **Step 5: gates + commit** (`feat(3b4): migration 0996 + shadow store contracts + two-phase writer (task d1)`).

---

### Task D2: Telemetry flag + dispatch manifest + capture wiring

**Files:** Modify `planner/shadow.py`, `api/routes/contract.py`; Test `test_shadow_capture.py` + route test.

- [ ] **Step 1: tests (red)** — (a) telemetry off → zero rows, response byte-identical; (b) telemetry on + compile off → a dispatch row + one run_result per eligible recipe with `compile_status='skipped'`... wait, `compile_status='not_applicable'` when not compiling — clarify: when compile off, `planner_outcome` still records the planning outcome, `compile_status='not_applicable'`; (c) **pre-loop failure**: monkeypatch `resolve_catalog_scope` to raise → the dispatch manifest still persists + a `run_result` with `planner_outcome='preloop_failure'` per eligible recipe (or one run-level marker), `run_shadow_planner` returns normally, the route savepoint commits; (d) `template_not_found` for an eligible id with no template → a run_result row (not a silent skip); (e) reconcile: manifest expected_count == run_result count.
- [ ] **Step 2–4: implement.**
  - `run_shadow_planner(conn, *, eligible_recipe_ids, target_entity, roles, run_id, now, compile_contracts, persist)` — new `persist: bool`. When `persist`: FIRST write the dispatch manifest (`eligible_recipe_ids` + hash + `expected_count` + `invocation_predicate` + `compile_flag`(=compile_contracts) + `telemetry_flag`(=True) + versions + producer_commit), BEFORE `resolve_catalog_scope`, WITHOUT `catalog_scope_id`. Then wrap `resolve_catalog_scope` + `build_compiler_context` in a **nested savepoint caught internally** → on failure, write one `run_result` per eligible recipe with `planner_outcome='preloop_failure'`, `compile_status='not_applicable'`, and RETURN normally (never propagate). Then the recipe loop: per recipe, compute the result (existing), map to a `RunResultRowV1` (+ `PlanObservationRowV1` per candidate), `write_run_and_plans`; a `template_not_found` id → a run_result with that outcome. Catalog_scope_id goes on the run_result rows.
  - `contract.py` — read `telemetry = os.environ.get("FEATUREGEN_INTENT_SHADOW_TELEMETRY","0")=="1"`; pass `persist=telemetry`. The dispatch manifest is written whenever `telemetry` (independent of the compile flag — F3). Keep the outer route savepoint + the existing compile-flag read.
- [ ] **Step 5: gates + commit** (`feat(3b4): telemetry flag + dispatch manifest + capture-integrity wiring (task d2)`).

---

### Task D3: The two inputs-only hashes + stamp enrichment (algorithmic core)

**Files:** Create `planner/fingerprint.py`; Modify `contracts.py` (`CatalogStateStampV1` + `ROLE_RESOLUTION_VERSION`); Test `test_fingerprint.py`.

**Produces:** `planner_input_hash(ctx, template, scope) -> str` (full candidate/ranking universe); `contract_input_hash(ctx, plan) -> str` (selected plan's consumed inputs); `declarations_output_hash(plan) -> str` (SEPARATE — outputs); enriched `CatalogStateStampV1` (+ `compiler_input_fingerprint`, `projection_checkpoint`).

- [ ] Tests: **additivity/is_as_of/sensitivity change on a bound column → `contract_input_hash` changes** (proves the `_Col` fields are hashed, not just `realization_fingerprint`); **a NEW candidate column for a need → `planner_input_hash` changes but the old selected plan's `contract_input_hash` does NOT** (F5 — selection vs verdict identity); **an output/declaration change under FIXED inputs → the input hashes are unchanged but `declarations_output_hash` changes** (F4 — instability is detectable, inputs aren't polluted); determinism (same inputs → identical hashes); real `ROLE_RESOLUTION_VERSION` (not "unknown").
- [ ] Implement: `planner_input_hash` = canonical hash over EVERY authorized `_Col` row from `ctx.columns_by_catalog` (fields `additivity,is_as_of,entity,sensitivity,concept,is_grain,data_type,object_ref` sorted) + all realizations + the scope-filtered bridge fact-key set + `sorted(scope.authorized_catalog_sources)` + read-scope roles + the version set. `contract_input_hash` = the same but restricted to the SELECTED plan's read-set columns + used realizations (`realization_ref`s in its segments) + used bridges (`bridge_fact_key`s). BOTH exclude any declaration/verdict field. `declarations_output_hash` = canonical hash of `hop_aggregations` + `temporal_declaration` + `physical_read_set` + `declaration_status` + `contract_reason_codes`. Enrich `CatalogStateStampV1` with `compiler_input_fingerprint: str` + `projection_checkpoint: int`. Set `ROLE_RESOLUTION_VERSION` to a real version; add a producer-commit/config-hash source.
- [ ] gates + commit (`feat(3b4): dual inputs-only fingerprints (planner + contract) + stamp enrichment (task d3)`).

---

### Task D4: `ReplayFreshness` (pure comparator + impure adapter)

**Files:** Create `planner/replay.py`; Test `test_replay.py`.

**Produces:** `compare_freshness(stored_stamp, current_evidence) -> ReplayFreshness` (PURE); `read_current_evidence(conn, catalogs, versions) -> CurrentEvidenceV1` (IMPURE, snapshot-consistent); `replay_freshness(conn, stored) -> ReplayFreshness`.

- [ ] Tests: matching fingerprints + checkpoint≥head → `current`; a realization fingerprint change (incl. additivity — reuse D3's compiler_input_fingerprint) → `drifted`; **unrelated projection checkpoint ADVANCEMENT → NOT drifted** (checkpoint is a lag invariant `≥ head_seq`, not equality — F4); **out-of-scope bridge change → NOT drifted** (scope-filtered set); **compiler/registry VERSION mismatch → `incompatible`** (NOT drifted — F15); a missing/incomplete stamp or `checkpoint < head_seq` → `unverifiable`; `unverifiable`/`incompatible` are NEVER `current`.
- [ ] Implement the pure comparator + the impure adapter (reads under a consistent snapshot / revalidation). Version mismatch short-circuits to `incompatible` before any drift comparison.
- [ ] gates + commit (`feat(3b4): ReplayFreshness — pure comparator + impure snapshot adapter (task d4)`).

---

### Task D5: Two-layer cause taxonomy

**Files:** Create `planner/cause.py`; Test `test_cause.py`.

**Produces:** `ReasonCategory` + `RESOLUTION_CATEGORY_MAP` (versioned) + `assert_map_exhaustive()`; `ResolutionCause` (Layer B); `category_of(reason) -> ReasonCategory`; `contextual_cause(observation, evidence, expert_label) -> ResolutionCause`.

- [ ] Tests: **Layer-A map is exhaustive over the WHOLE `ReasonCode` registry** — a static test iterates every `ReasonCode` member and asserts a category (an unmapped member → `operationally_unmeasured`, and the test FAILS if any is unmapped); `safety_rejected`/topology map to their categories (NOT `internal`); `operationally_unmeasured` (unmapped) is distinct from `unknown` (mapped, Layer-B-pending); Layer-B `classifier_defect` requires an expert label (never inferred from the code).
- [ ] Implement the versioned static map + the exhaustiveness assertion + the Layer-B contextual classifier over a deduplicated (reason+evidence-shape) key.
- [ ] gates + commit (`feat(3b4): two-layer cause taxonomy (static exhaustive + contextual expert) (task d5)`).

---

### Task D6: Defect folds — compile_status + monotonic budget + take_latest

**Files:** Modify `planner/plan.py`, `shadow.py`, `declarations.py`, `contracts.py`; Test `test_declarations.py`/`test_plan.py`.

- [ ] Tests: **real elapsed-time budget timeout** — an injectable monotonic clock (not the deterministic `now`) whose advance past the deadline sets `compile_status=incomplete`/`incomplete_reason=budget_time` (the count-only budget was inert — F12); a budget-incomplete run is EXCLUDED from deterministic-verdict comparisons; `compile_status=complete` ONLY when every PATH-RESOLVED candidate compiled (F10 — eligibility = source_to_target_resolved); **`take_latest` with an ordering column NOT available at the aggregation hop (or aggregated away by a prior hop) → `aggregation_ordering_column_missing`** (F14 — `anchor_binding is not None` is insufficient); with an available+surviving ordering column → sound.
- [ ] Implement: an injectable `monotonic()` clock threaded into `CompileBudget` (the deadline uses it; it does NOT enter any hash/verdict); `plan_bindings` records `compile_status`/`incomplete_reason` + the eligible/compiled/skipped counts on the result; the `_validate_stage` take_latest guard checks the anchor's `placement` position ≤ the stage hop AND survival through prior grouping keys, and adds the ordering column to the stage's physical-read set + safety evidence.
- [ ] gates + commit (`feat(3b4): defect folds — compile_status + monotonic budget + take_latest stage-local ordering (task d6)`).

---

### Task D7: Curated gold set + `contract_eval`

**Files:** Create `planner/contract_gold.py`, `planner/contract_eval.py`; Test `test_contract_eval.py`.

**Produces:** `GOLD_SET` (versioned, content-hashed, immutable sample IDs + expert labels); `evaluate(conn) -> EvalResultV1` (exact-match + strict false-resolve); `sample_audit(conn, seed, strata, per_stratum) -> AuditFrameV1` (dedup by `contract_input_hash`, shape-weighted); the double-compile stability procedure.
- [ ] Tests: exact-match verdict+cause on every gold case; a gold case labelled invalid but classified `resolved` → FAILURE; the audit sampler dedups repeated `contract_input_hash` (clustered traffic doesn't inflate); a rare stratum (< `per_stratum` distinct shapes) is flagged; **double-compile from the same frozen fixture → identical** id/verdict; an empty comparison set → the stability check FAILS.
- [ ] Implement the gold corpus (seeded fixtures for the adversarial shapes) + `evaluate` + the seeded stratified sampler + the double-compile procedure. `GOLD_SET_HASH` is a module-level content hash.
- [ ] gates + commit (`feat(3b4): curated gold set + contract_eval + stratified audit sampler (task d7)`).

---

### Task D8: Population report + conjunctive gate + detached-signed artifact

**Files:** Create `planner/shadow_report.py`; Test `test_shadow_report.py`.

**Produces:** `population_report(conn, window, cohort) -> PopulationReportV1` (the §9 schema); `evaluate_gate(report, gold, audit, policy) -> GateResultV1` (7 sub-gates); `sign_report(report, signer) / verify_report(path) -> bool` (detached signature); a runnable `main()` with nonzero exit on failure.

- [ ] Tests: the report's exact numerator/denominator (selected + `compile_status=complete` + `source_to_target_resolved`; one obs per (run,recipe)); multi-reason counting (headline by-primary, breakdown by-each dimension/category); **conjunctive gate — any single sub-gate failing → GateResult fails** (no averaging); **incomplete eligible recipes fail gate 1** (unless signed-excluded); zero `operationally_unmeasured`/`classifier_defect`/`unknown` required; the **statistical bound** — future-traffic binomial Clopper-Pearson upper bound (NO finite-population correction), zero-failure over ~300 distinct shapes → ≤1% one-sided 95%; a rare stratum below the sample size → FAIL; **replay-stability** uses D7's double-compile (empty set → fail); **a human cannot override a FAILED machine gate**; **signed-report tampering → `verify_report` False + nonzero exit**; the artifact records commit/gold-hash/policy-hash/versions/window/sample-ids/signer.
- [ ] Implement `population_report` (the §9 contract exactly), the 7-gate evaluator (machine gates 1/2a/5/6 computed; human gates 2b/3/4/7 consume the labelled artifacts; a failed machine gate can't be overridden), the binomial-bound machinery (`upper_bound(n, failures, conf)`; the required-n solver; per-stratum), the detached signature (sign a canonical digest with a signer key; `verify_report` recomputes + checks; NOT self-signed), and `main()` (nonzero exit).
- [ ] **Behaviour-neutral proof** — `uv run pytest tests/featuregen/ -q` green; both flags off → no writes, response byte-identical.
- [ ] gates + commit (`feat(3b4): population report + conjunctive signed enablement gate (task d8)`).

---

## Exit criteria mapping

| Spec (v3) | Task |
|---|---|
| Telemetry flag; manifest-before-scope; nested savepoint; two-phase persist; WORM+TRUNCATE; three axes | D1, D2 |
| Two inputs-only hashes (selection vs verdict); stamp enrichment; real ROLE_RESOLUTION_VERSION | D3 |
| ReplayFreshness (checkpoint lag; scope-filtered bridge; incompatible≠drift) | D4 |
| Two-layer cause taxonomy (exhaustive + contextual) | D5 |
| compile_status(path-resolved) + Gate-1 completeness; monotonic budget; take_latest stage-local | D6, D8 |
| Gold set + false-resolve + stratified audit + double-compile stability | D7 |
| Population report schema; 7-gate conjunctive; binomial bound; detached signature; no-override | D8 |
| Behaviour-neutral (both flags off) | D2, D8 |

## Self-Review

**Spec coverage:** every v3 section maps to a task (table). ✅
**Placeholder scan:** D1 carries full DDL + the two-phase writer; D2–D8 carry exact signatures + the mandatory tests as the behavioural contract (D3 fingerprints + D8 statistics/signature are algorithmic/security — deepest review). Deliberate, flagged. ✅
**Type consistency:** the store row dataclasses (D1) are produced by D2's wiring and consumed by D8's report; `contract_input_hash` (D3) is persisted (D1/D2) + the audit sampling unit (D7) + the drift signal (D4); `compile_status` (D6) gates the denominator (D8) + Gate 1; `ResolutionCause`/`ReasonCategory` (D5) feed the report + Gate 2 (D8). ✅
**Executor notes:** (1) both flags off = ZERO writes + byte-identical response is the hard behaviour-neutral invariant (D2/D8 must prove it). (2) The manifest MUST be written before `resolve_catalog_scope` and WITHOUT `catalog_scope_id`; a pre-loop failure returns normally (never propagates) so the route savepoint retains it. (3) Hashes are INPUTS-ONLY — declarations are outputs, hashed separately (D3). (4) The gate evaluator must NOT let a human override a failed MACHINE gate, and the evaluator must NOT sign its own output (D8). (5) The monotonic budget clock must NOT enter any hash/verdict (operational only) — determinism of the verdict is preserved; budget-incomplete runs are excluded from stability comparisons.
